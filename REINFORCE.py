import gym
import matplotlib.pyplot as plt
import numpy
import torch
import pyro
import tqdm

import utils.common
import utils.envs
import utils.seed
import utils.torch

import warnings
warnings.filterwarnings("ignore")


# Deep Q Learning
# Slide 14
# cs.uwaterloo.ca/~ppoupart/teaching/cs885-spring20/slides/cs885-lecture4b.pdf

class REINFORCE(torch.nn.Module):
    def __init__(
            self,
            MODE,
            SMOKE_TEST=False,
            ENV_NAME="CartPole-v0",
            GAMMA=0.99,
            # Discount factor in episodic reward objective
            MINIBATCH_SIZE=64,
            # How many examples to sample per train step
            HIDDEN=512,
            # Hiddien states
            LEARNING_RATE=5e-4,
            # Learning rate for Adam optimizer
            SEEDS=[1, 2, 3, 4, 5],
            # Randoms seeds for mutiple trails
            EPISODES=300 * 25,
            # Total number of episodes to learn over
            TEMPERATURE=None
    ):
        super().__init__()
        self.t = utils.torch.TorchHelper()
        # Constants
        self.DEVICE = self.t.device
        self.ENV_NAME = ENV_NAME
        self.GAMMA = GAMMA
        self.MINIBATCH_SIZE = MINIBATCH_SIZE
        self.LEARNING_RATE = LEARNING_RATE
        self.HIDDEN = HIDDEN
        self.MODE = MODE

        if SMOKE_TEST:
            self.SEEDS = [1, 2]
            self.EPISODES = 20
        else:
            self.SEEDS = SEEDS
            self.EPISODES = EPISODES

        assert (self.SOFT_OFF != self.SOFT_ON)
        assert (self.SOFT_ON <= (TEMPERATURE != None))
        assert (self.SOFT_OFF <= (TEMPERATURE == None))
        self.TEMPERATURE = TEMPERATURE

    @property
    def SVI_ON(self):
        return self.MODE == "pyro"
    @property
    def SOFT_ON(self):
        return self.MODE == "pyro" or self.MODE == "soft"
    @property
    def SOFT_OFF(self):
        return self.MODE == "hard"
    def create_everything(self, seed):
        utils.seed.seed(seed)
        env = gym.make(self.ENV_NAME)
        env.seed(seed)
        test_env = gym.make(self.ENV_NAME)
        test_env.seed(10+seed)

        assert (isinstance(env.action_space,gym.spaces.discrete.Discrete))
        assert (isinstance(env.observation_space,gym.spaces.box.Box))
        self.OBS_N = env.observation_space.shape[0]
        self.ACT_N = env.action_space.n
        self.unif = torch.ones(self.ACT_N) / self.ACT_N

        self.pi = torch.nn.Sequential(
            torch.nn.Linear(self.OBS_N, self.HIDDEN), torch.nn.ReLU(),
            torch.nn.Linear(self.HIDDEN, self.HIDDEN), torch.nn.ReLU(),
            torch.nn.Linear(self.HIDDEN, self.ACT_N),
            torch.nn.Softmax()
        ).to(self.DEVICE)

        if self.SVI_ON:
            adma = pyro.optim.Adam({"lr":self.LEARNING_RATE})
            OPT = pyro.infer.SVI(self.model, self.guide, adma, loss=pyro.infer.Trace_ELBO())
        else:
            OPT = torch.optim.Adam(self.pi.parameters(), lr = self.LEARNING_RATE)

        return env, test_env, self.pi, OPT

    def guide(self, env = None):
        pyro.module("agentmodel", self)
        time_stamp = 0
        states, total_reward = [], 0
        obs = env.reset()
        done = False
        while not done:
            states.append(obs)
            action = pyro.sample(
                "action_{}".format(time_stamp),
                pyro.distributions.Categorical(self.pi(self.t.f(obs)))
            ).item()
            obs, reward, done, info = env.step(action)
            total_reward += reward
            time_stamp += 1
        states.append(obs)
        self.traj = (states, total_reward)

    def model(self, env = None):
        S, total_reward = self.traj
        for idx, state in enumerate(S[:-1]):
            action = pyro.sample(
                "action_{}".format(idx),
                pyro.distributions.Categorical(self.unif)
            )
        pyro.factor("total_reward", total_reward / self.TEMPERATURE)

    def train(self, seed):

        print("Seed=%d" % seed)
        env, test_env, pi, OPT = self.create_everything(seed)

        if self.SVI_ON:
            pyro.clear_param_store()

        def policy(env, obs):
            with torch.no_grad():
                obs = self.t.f(obs).view(-1, self.OBS_N)  # Convert to torch tensor
                action = torch.distributions.Categorical(pi(obs)).sample().item()
            return action

        trainRs = []
        last25Rs = []
        print("Training:")
        pbar = tqdm.trange(self.EPISODES)
        for epi in pbar:
            if self.SVI_ON:
                OPT.step(env)
                trainRs += [self.traj[-1]]
            else:
                # Play an episode and log episodic reward

                S, A, R = utils.envs.play_episode_tensor(env, policy)

                nSteps = len(S)

                if self.MODE == "soft":
                    with torch.no_grad():
                        R -= self.TEMPERATURE * torch.log(pi(S[:-1]).gather(-1, A.view(-1,1))).squeeze()

                G = torch.zeros(nSteps)
                G[-1] = R[-1]
                for step in reversed(range(nSteps - 1)):
                    G[step] = R[step] + self.GAMMA * G[step + 1]

                adv = torch.tensor([(self.GAMMA ** step) * G[step] for step in range(nSteps - 1)])
                loss = - adv * torch.log(pi(S[:-1]).gather(-1, A.view(-1,1))).squeeze()
                OPT.zero_grad()
                loss.mean().backward()
                OPT.step()

                trainRs += [G[0]]
                # Update progress bar
            last25Rs += [sum(trainRs[-25:])/len(trainRs[-25:])]
            pbar.set_description("R25(%g)" % (last25Rs[-1]))

        # Close progress bar, environment
        pbar.close()
        print("Training finished!")
        env.close()
        test_env.close()

        return last25Rs

    def run(self, label=""):
        # Train for different seeds
        filename = utils.common.safe_filename(f"REINFORCE-{self.MODE}{label}-{self.ENV_NAME}-SEED={self.SEEDS}-TEMPERATURE={self.TEMPERATURE}")
        curves = [self.train(seed) for seed in self.SEEDS]
        with open(f'{filename}.csv', 'w') as csv:
            numpy.savetxt(csv, numpy.asarray(curves), delimiter=',')
        # Plot the curve for the given seeds
        plt.figure(dpi=120)
        x = range(self.EPISODES)
        if label == None:
            label = self.MODE
        utils.common.plot_arrays(x, curves, 'b', label)
        plt.legend(loc='best')
        plt.savefig(f'{filename}.png')
        plt.show()

if __name__ == "__main__":
    reinforece = REINFORCE(
        "hard",
        SMOKE_TEST=True
        # TEMPERATURE=1
        # SEEDS=[1,2],
        # EPISODES=50
    )
    reinforece.run()