import copy
import time
import torch
import numpy as np
from torch import optim
from Agents.Base_Agent import Base_Agent
from Neural_Network import Neural_Network
from Parallel_Experience_Generator import Parallel_Experience_Generator
from Utility_Functions import normalise_rewards, create_actor_distribution


class PPO_Agent(Base_Agent):
    agent_name = "PPO"

    def __init__(self, config):
        Base_Agent.__init__(self, config)
        self.initialise_policies()
        self.max_steps_per_episode = config.environment.get_max_steps_per_episode()
        self.policy_new_optimizer = optim.Adam(self.policy_new.parameters(), lr=self.hyperparameters["learning_rate"])
        self.episode_number = 0
        self.many_episode_states = []
        self.many_episode_actions = []
        self.many_episode_rewards = []

    def initialise_policies(self):
        """Initialises the policies"""
        if self.action_types == "DISCRETE":
            policy_output_size = self.action_size
        else:
            policy_output_size = self.action_size * 2 #Because we need 1 parameter for mean and 1 for std of distribution

        self.policy_new = Neural_Network(self.state_size, policy_output_size, self.random_seed, self.hyperparameters).to(
            self.device)
        self.policy_old = Neural_Network(self.state_size, policy_output_size, self.random_seed, self.hyperparameters).to(
            self.device)

    def run_n_episodes(self, num_episodes_to_run=1, save_model=False):
        """Runs game to completion n times and then summarises results and saves model (if asked to)"""

        start = time.time()
        obj = Parallel_Experience_Generator(self.environment, self.policy_new)

        while self.episode_number < num_episodes_to_run:
            self.many_episode_states, self.many_episode_actions, self.many_episode_rewards = obj.play_n_episodes(self.hyperparameters["episodes_per_learning_round"])
            self.episode_number += self.hyperparameters["episodes_per_learning_round"]
            self.save_and_print_result()

            if self.max_rolling_score_seen > self.average_score_required_to_win:  # stop once we achieve required score
                break

            for _ in range(self.hyperparameters["learning_iterations_per_round"]):
                self.policy_learn()

            self.update_learning_rate(self.hyperparameters["learning_rate"], self.policy_new_optimizer)

            self.equalise_policies()

        time_taken = time.time() - start
        return self.game_full_episode_scores, self.rolling_results, time_taken

    def policy_learn(self):

        all_ratio_of_policy_probabilities = []
        all_discounted_returns = []

        for episode in range(len(self.many_episode_states)):
            for ix in range(len(self.many_episode_states[episode])):
                discounted_return = self.calculate_discounted_reward(self.many_episode_rewards[episode][ix:])
                all_discounted_returns.append(discounted_return)

                ratio_of_policy_probabilities = self.calculate_policy_action_probability_ratio(self.many_episode_states[episode][ix], self.many_episode_actions[episode][ix])
                all_ratio_of_policy_probabilities.append(ratio_of_policy_probabilities)

        if self.hyperparameters["normalise_rewards"]:
            all_discounted_returns = normalise_rewards(all_discounted_returns)

        loss = self.calculate_loss(all_ratio_of_policy_probabilities, all_discounted_returns)
        self.take_policy_new_optimisation_step(loss)

    def calculate_discounted_reward(self, rewards):
        discounts = self.hyperparameters["discount_rate"] ** np.arange(len(rewards))
        total_discounted_reward = np.dot(discounts, rewards)
        return total_discounted_reward

    def calculate_policy_action_probability_ratio(self, state, action):

        state = torch.from_numpy(state).float().unsqueeze(0).to(self.device)

        new_policy_output = self.policy_new.forward(state).cpu()
        old_policy_output = self.policy_old.forward(state).cpu()

        new_policy_distribution = create_actor_distribution(self.action_types, new_policy_output, self.action_size)
        old_policy_distribution = create_actor_distribution(self.action_types, old_policy_output, self.action_size)

        new_policy_distribution_log_prob = new_policy_distribution.log_prob(torch.from_numpy(np.array(action)))
        old_policy_distribution_log_prob = old_policy_distribution.log_prob(torch.from_numpy(np.array(action)))

        ratio_of_policy_probabilities = torch.exp(new_policy_distribution_log_prob) / torch.exp(old_policy_distribution_log_prob)

        return ratio_of_policy_probabilities

    def calculate_loss(self, all_ratio_of_policy_probabilities, all_discounted_returns):

        clipped_probability_ratios = [torch.clamp(value, min=1.0 - self.hyperparameters["clip_epsilon"],
                                                    max=1.0 + self.hyperparameters["clip_epsilon"]) for value in all_ratio_of_policy_probabilities]
        observation_losses = [torch.min(policy_probability_ratio * discounted_return, clipped_probability_ratio * discounted_return)
                              for policy_probability_ratio, discounted_return, clipped_probability_ratio
                              in zip(all_ratio_of_policy_probabilities, all_discounted_returns, clipped_probability_ratios) ]
        loss = - torch.sum(torch.stack(observation_losses))

        average_loss = loss / len(all_ratio_of_policy_probabilities)

        return average_loss

    def take_policy_new_optimisation_step(self, loss):
        self.policy_new.zero_grad()  # reset gradients to 0
        loss.backward()  # this calculates the gradients
        self.policy_new_optimizer.step()  # this applies the gradients

    def equalise_policies(self):
        """Sets the old policy's parameters equal to the new policy's parameters"""
        for old_param, new_param in zip(self.policy_old.parameters(), self.policy_new.parameters()):
            old_param.data.copy_(new_param.data)

    def save_result(self):
        for ep in range(len(self.many_episode_rewards)):
            total_reward = np.sum(self.many_episode_rewards[ep])
            self.game_full_episode_scores.append(total_reward)
            self.rolling_results.append(np.mean(self.game_full_episode_scores[-1 * self.rolling_score_window:]))
        self.save_max_result_seen()