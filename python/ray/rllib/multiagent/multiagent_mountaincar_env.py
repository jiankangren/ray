import math
import gym
from gym.spaces import *
from gym.utils import seeding
import numpy as np
from gym.envs.classic_control.mountain_car import MountainCarEnv

class MultiAgentMountainCarEnv(MountainCarEnv):
    def __init__(self):
        self.min_position = -1.2
        self.max_position = 0.6
        self.max_speed = 0.07
        self.goal_position = 0.5

        self.low = np.array([self.min_position, -self.max_speed])
        self.high = np.array([self.max_position, self.max_speed])

        self.viewer = None

        self.action_space = [Discrete(3) for _ in range(2)]
        self.observation_space = Tuple(tuple(Box(self.low, self.high) for _ in range(2)))

        self._seed()
        self.reset()

    def _step(self, action):
        summed_act = 0.5*np.sum(action)
        #assert self.action_space.contains(summed_act), "%r (%s) invalid" % (summed_act, type(summed_act))

        position, velocity = self.state
        velocity += (summed_act-1)*0.001 + math.cos(3*position)*(-0.0025)
        velocity = np.clip(velocity, -self.max_speed, self.max_speed)
        position += velocity
        position = np.clip(position, self.min_position, self.max_position)
        if (position==self.min_position and velocity<0): velocity = 0

        done = bool(position >= self.goal_position)
        reward = -1.0

        self.state = (position, velocity)
        return [np.array(self.state) for _ in range(2)], reward, done, {}

    def _reset(self):
        self.state = np.array([self.np_random.uniform(low=-0.6, high=-0.4), 0])
        return [np.array(self.state) for _ in range(2)]