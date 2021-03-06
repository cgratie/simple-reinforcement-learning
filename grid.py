#!/usr/bin/env python3

# Copyright 2016 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# TODO:
# - Decouple learning from the animated display
# - Implement random maps and approximate value functions

import collections
import curses
import random
import sys
import time
import unittest

from unittest.mock import patch

# Grid world maps are specified with characters a bit like NetHack:
# #, (blank) are impassable
# . is passable
# @ is the player start point
# ^ is a trap, with a large negative reward
# $ is the goal
VALID_CHARS = set(['#', '.', '@', '$', '^', ' '])

class WorldFailure(Exception):
  pass

class World(object):
  '''A grid world.'''
  def __init__(self, init_state, lines):
    '''Creates a grid world.
    init_state: the (x,y) player start position
    lines: list of strings of VALID_CHARS, the map'''
    self.init_state = init_state
    self._lines = [] + lines

  @classmethod
  def parse(cls, s):
    '''Parses a grid world in the string s.
s must be made up of equal-length lines of VALID_CHARS with one start position
denoted by @.'''
    init_state = None

    lines = s.split()
    if not lines:
      raise WorldFailure('no content')
    for (y, line) in enumerate(lines):
      if y > 0 and len(line) != len(lines[0]):
        raise WorldFailure('line %d is a different length (%d vs %d)' %
                                (y, len(line), len(lines[0])))
      for (x, ch) in enumerate(line):
        if not ch in VALID_CHARS:
          raise WorldFailure('invalid char "%c" at (%d, %d)' % (ch, x, y))
        if ch == '@':
          if init_state:
            raise WorldFailure('multiple initial states, at %o and '
                               '(%d, %d)' % (init_state, x, y))
          init_state = (x, y)
    if not init_state:
      raise WorldFailure('no initial state, use "@"')
    # The player start position is in fact ordinary ground.
    x, y = init_state
    line = lines[y]
    lines[y] = line[0:x] + '.' + line[x+1:]
    return World(init_state, lines)

  @property
  def size(self):
    '''The size of the grid world, width by height.'''
    return self.w, self.h

  @property
  def h(self):
    '''The height of the grid world.'''
    return len(self._lines)

  @property
  def w(self):
    '''The width of the grid world.'''
    return len(self._lines[0])

  def at(self, pos):
    '''Gets the character at an (x, y) coordinate.
Positions are indexed from the origin 0,0 at the top, left of the map.'''
    x, y = pos
    return self._lines[y][x]


class TestWorld(unittest.TestCase):
  def test_size(self):
    g = World.parse('@$')
    self.assertEqual((2, 1), g.size)

  def test_init_state(self):
    g = World.parse('####\n#.@#\n####')
    self.assertEqual((2, 1), g.init_state)

  def test_parse_no_init_state_fails(self):
    with self.assertRaises(WorldFailure):
      World.parse('#')


# The player can take four actions: move up, down, left or right.
ACTION_UP = 'u'
ACTION_DOWN = 'd'
ACTION_LEFT = 'l'
ACTION_RIGHT = 'r'

MOVEMENT = {
  ACTION_UP: (0, -1),
  ACTION_DOWN: (0, 1),
  ACTION_LEFT: (-1, 0),
  ACTION_RIGHT: (1, 0)
}

ALL_ACTIONS = list(MOVEMENT.keys())

class Simulation(object):
  '''Tracks the player in a world and implements the rules and rewards.
score is the cumulative score of the player in this run of the simulation.'''
  def __init__(self, world):
    '''Creates a new simulation in world.'''
    self._world = world
    self.reset()

  def reset(self):
    '''Resets the simulation to the initial state.'''
    self.state = self._world.init_state
    self.score = 0

  @property
  def in_terminal_state(self):
    '''Whether the simulation is in a terminal state (stopped.)'''
    return self._world.at(self.state) in ['^', '$']

  @property
  def x(self):
    '''The x coordinate of the player.'''
    return self.state[0]

  @property
  def y(self):
    '''The y coordinate of the player.'''
    return self.state[1]

  def act(self, action):
    '''Performs action and returns the reward from that step.'''
    reward = -1

    delta = MOVEMENT[action]
    new_state = self.x + delta[0], self.y + delta[1]

    if self._valid_move(new_state):
      if self._world.at(new_state) == '^':
        reward = -1000
      self.state = new_state

    self.score += reward
    return reward

  def _valid_move(self, new_state):
    '''Gets whether movement to new_state is a valid move.'''
    new_x, new_y = new_state
    # TODO: Could check that there's no teleportation cheating.
    return (0 <= new_x and new_x < self._world.w and
            0 <= new_y and new_y < self._world.h and
            self._world.at(new_state) in ['.', '^', '$'])


class TestSimulation(unittest.TestCase):
  def test_in_terminal_state(self):
    world = World.parse('@^')
    sim = Simulation(world)
    self.assertFalse(sim.in_terminal_state)
    sim.act(ACTION_RIGHT)
    self.assertTrue(sim.in_terminal_state)

  def test_act_accumulates_score(self):
    world = World.parse('@.')
    sim = Simulation(world)
    sim.act(ACTION_RIGHT)
    sim.act(ACTION_LEFT)
    self.assertEqual(-2, sim.score)

# There is also an interactive version of the game. These are keycodes
# for interacting with it.
KEY_Q = ord('q')
KEY_ESC = 27
KEY_SPACE = ord(' ')
KEY_UP = 259
KEY_DOWN = 258
KEY_LEFT = 106
KEY_RIGHT = 107
KEY_ACTION_MAP = {
  KEY_UP: ACTION_UP,
  KEY_DOWN: ACTION_DOWN,
  KEY_LEFT: ACTION_LEFT,
  KEY_RIGHT: ACTION_RIGHT
}
QUIT_KEYS = set([KEY_Q, KEY_ESC])


class Game(object):
  '''A simulation that uses curses.'''
  def __init__(self, world, driver):
    '''Creates a new game in world where driver will interact with the game.'''
    self._world = world
    self._sim = Simulation(world)
    self._driver = driver

  def start(self):
    '''Sets up and starts the game and runs it until the driver quits.'''
    curses.initscr()
    curses.wrapper(self._loop)

  # The game loop.
  def _loop(self, window):
    while not self._driver.should_quit:
      # Paint
      self._draw(window)
      window.addstr(self._world.h, 0, 'Score: %d' % self._sim.score)
      window.move(self._sim.y, self._sim.x)
      window.refresh()

      # Get input, etc.
      self._driver.interact(self._sim, window)

  # Paints the window.
  def _draw(self, window):
    window.erase()
    # Draw the environment
    for y, line in enumerate(self._world._lines):
      window.addstr(y, 0, line)
    # Draw the player
    window.addstr(self._sim.y, self._sim.x, '@')


class HumanPlayer(object):
  '''A game driver that reads input from the keyboard.'''
  def __init__(self):
    self._ch = 0

  @property
  def should_quit(self):
    return self._ch in QUIT_KEYS

  def interact(self, sim, window):
    self._ch = window.getch()
    if self._ch in KEY_ACTION_MAP and not sim.in_terminal_state:
      sim.act(KEY_ACTION_MAP[self._ch])
    elif self._ch == KEY_SPACE and sim.in_terminal_state:
      sim.reset()


class MachinePlayer(object):
  '''A game driver which applies a policy, observed by a learner.
The learner can adjust the policy.'''
  def __init__(self, policy, learner):
    self._policy = policy
    self._learner = learner

  @property
  def should_quit(self):
    return False

  def interact(self, sim, window):
    if sim.in_terminal_state:
      time.sleep(1)
      sim.reset()
    else:
      old_state = sim.state
      action = self._policy.pick_action(sim.state)
      reward = sim.act(action)
      self._learner.observe(old_state, action, reward, sim.state)
      time.sleep(0.05)


class StubFailure(Exception):
  pass


class StubWindow(object):
  '''A no-op implementation of the game display.'''
  def addstr(self, y, x, s):
    pass

  def erase(self):
    pass

  def getch(self):
    raise StubFailure('"getch" not implemented; use a mock')

  def move(self, y, x):
    pass

  def refresh(self):
    pass


class StubLearner(object):
  '''Plugs in as a learner but doesn't update anything.'''
  def observe(self, old_state, action, reward, new_state):
    pass


class TestMachinePlayer(unittest.TestCase):
  def test_interact(self):
    TEST_ACTION = ACTION_RIGHT
    q = QTable(-1)
    q.set((0, 0), TEST_ACTION, 1)

    player = MachinePlayer(GreedyQ(q), StubLearner())
    world = World.parse('@.')
    with patch.object(Simulation, 'act') as mock_act:
      sim = Simulation(world)
      player.interact(sim, StubWindow())
    mock_act.assert_called_once_with(TEST_ACTION)

  def test_does_not_quit(self):
    player = MachinePlayer(None, None)
    self.assertFalse(player.should_quit)


class RandomPolicy(object):
  '''A policy which picks actions at random.'''
  def pick_action(self, _):
    return random.choice(ALL_ACTIONS)


class EpsilonPolicy(object):
  '''Pursues policy A, but uses policy B with probability epsilon.

Be careful when using a learned function for one of these policies;
the epsilon policy needs an off-policy learner.
  '''
  def __init__(self, policy_a, policy_b, epsilon):
    self._policy_a = policy_a
    self._policy_b = policy_b
    self._epsilon = epsilon

  def pick_action(self, state):
    if random.random() < self._epsilon:
      return self._policy_b.pick_action(state)
    else:
      return self._policy_a.pick_action(state)


class QTable(object):
  '''An approximation of the Q function based on a look-up table.
  As such it is only appropriate for discrete state-action spaces.'''
  def __init__(self, init_reward = 0):
    self._table = collections.defaultdict(lambda: init_reward)

  def get(self, state, action):
    return self._table[(state, action)]

  def set(self, state, action, value):
    self._table[(state, action)] = value

  def best(self, state):
    '''Gets the best predicted action and its value for |state|.'''
    best_value = -1e20
    best_action = None
    for action in ALL_ACTIONS:
      value = self.get(state, action)
      if value > best_value:
        best_action, best_value = action, value
    return best_action, best_value


class GreedyQ(object):
  '''A policy which chooses the action with the highest reward estimate.'''
  def __init__(self, q):
    self._q = q

  @property
  def should_quit(self):
    return False

  def pick_action(self, state):
    return self._q.best(state)[0]


class QLearner(object):
  '''An off-policy learner which updates a QTable.'''
  def __init__(self, q, learning_rate, discount_rate):
    self._q = q
    self._alpha = learning_rate
    self._gamma = discount_rate

  def observe(self, old_state, action, reward, new_state):
    prev = self._q.get(old_state, action)
    self._q.set(old_state, action, prev + self._alpha * (
      reward + self._gamma * self._q.best(new_state)[1] - prev))


def start(driver):
  world = World.parse('''\
########
#..#...#
#.@#.$.#
#.##^^.#
#......#
########
''')
  game = Game(world, driver)
  game.start()


def main():
  if '--interactive' in sys.argv:
    player = HumanPlayer()
  elif '--q' in sys.argv:
    q = QTable()
    learner = QLearner(q, 0.05, 0.1)
    policy = EpsilonPolicy(GreedyQ(q), RandomPolicy(), 0.01)
    player = MachinePlayer(policy, learner)
  else:
    print('use --test, --interactive or --q')
    sys.exit(1)
  start(player)


if __name__ == '__main__':
  if '--test' in sys.argv:
    del sys.argv[sys.argv.index('--test')]
    unittest.main()
  else:
    main()
