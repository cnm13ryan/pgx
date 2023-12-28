# Copyright 2023 The Pgx Authors. All Rights Reserved.
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

from functools import partial
from typing import NamedTuple, Optional

import jax
from jax import Array
from jax import numpy as jnp


class GameState(NamedTuple):
    color: Array = jnp.int32(0)  # 0 = black, 1 = white
    # ids of representative stone id (smallest) in the connected stones
    # positive for black, negative for white, and zero for empty.
    chain_id_board: Array = jnp.zeros(19 * 19, dtype=jnp.int32)
    board_history: Array = jnp.full((8, 19 * 19), 2, dtype=jnp.int32)  # mainly for obs
    num_captured_stones: Array = jnp.zeros(2, dtype=jnp.int32)  # [b, w]
    consecutive_pass_count: Array = jnp.int32(0)  # two consecutive pass ends the game
    ko: Array = jnp.int32(-1)  # by SSK
    is_psk: Array = jnp.bool_(False)

    @property
    def size(self) -> int:
        return int(jnp.sqrt(self.chain_id_board.shape[-1]).astype(jnp.int32).item())


class Game:
    def __init__(self, size: int = 19, komi: float = 7.5, history_length: int = 8):
        self.size = size
        self.komi = komi
        self.history_length = history_length

    def init(self) -> GameState:
        return GameState(
            chain_id_board=jnp.zeros(self.size**2, dtype=jnp.int32),
            board_history=jnp.full((8, self.size**2), 2, dtype=jnp.int32),
        )

    def step(self, state: GameState, action: Array) -> GameState:
        state = state._replace(ko=jnp.int32(-1))
        # update state
        state = jax.lax.cond(
            (action < self.size * self.size),
            lambda: _apply_action(state, action, self.size),
            lambda: _apply_pass(state),
        )
        # increment turns
        state = state._replace(color=(state.color + 1) % 2)
        # update board history
        board_history = jnp.roll(state.board_history, self.size**2)
        board_history = board_history.at[0].set(jnp.clip(state.chain_id_board, -1, 1).astype(jnp.int32))
        state = state._replace(board_history=board_history)
        # check PSK
        state = state._replace(is_psk=_check_PSK(state))
        return state

    def observe(self, state: GameState, color: Optional[Array] = None) -> Array:
        if color is None:
            color = state.color

        my_color_sign = jnp.int32([1, -1])[color]

        @jax.vmap
        def _make(i):
            c = jnp.int32([1, -1])[i % 2] * my_color_sign
            return state.board_history[i // 2] == c

        log = _make(jnp.arange(self.history_length * 2))
        color = jnp.full_like(log[0], color)  # black=0, white=1

        return jnp.vstack([log, color]).transpose().reshape((self.size, self.size, -1))

    def legal_action_mask(self, state: GameState) -> Array:
        """Logic is highly inspired by OpenSpiel's Go implementation"""
        is_empty = state.chain_id_board == 0

        my_color = _my_color(state)
        opp_color = _opponent_color(state)
        num_pseudo, idx_sum, idx_squared_sum = _count(state, self.size)

        chain_ix = jnp.abs(state.chain_id_board) - 1
        in_atari = (idx_sum[chain_ix] ** 2) == idx_squared_sum[chain_ix] * num_pseudo[chain_ix]
        has_liberty = (state.chain_id_board * my_color > 0) & ~in_atari
        kills_opp = (state.chain_id_board * opp_color > 0) & in_atari

        @jax.vmap
        def is_neighbor_ok(xy):
            neighbors = _neighbour(xy, self.size)
            on_board = neighbors != -1
            _has_empty = is_empty[neighbors]
            _has_liberty = has_liberty[neighbors]
            _kills_opp = kills_opp[neighbors]
            return (on_board & _has_empty).any() | (on_board & _kills_opp).any() | (on_board & _has_liberty).any()

        neighbor_ok = is_neighbor_ok(jnp.arange(self.size**2))
        legal_action_mask = is_empty & neighbor_ok

        legal_action_mask = jax.lax.cond(
            (state.ko == -1),
            lambda: legal_action_mask,
            lambda: legal_action_mask.at[state.ko].set(False),
        )
        return jnp.append(legal_action_mask, True)  # pass is always legal

    def is_terminal(self, state: GameState):
        two_consecutive_pass = state.consecutive_pass_count >= 2
        return two_consecutive_pass | state.is_psk

    def terminal_values(self, state: GameState):
        score = _count_point(state, self.size)
        reward_bw = jax.lax.select(
            score[0] - self.komi > score[1],
            jnp.array([1, -1], dtype=jnp.float32),
            jnp.array([-1, 1], dtype=jnp.float32),
        )
        to_play = state.color
        reward_bw = jax.lax.select(state.is_psk, jnp.float32([-1, -1]).at[to_play].set(1.0), reward_bw)
        return reward_bw


def _apply_pass(state: GameState) -> GameState:
    return state._replace(consecutive_pass_count=state.consecutive_pass_count + 1)


def _apply_action(state: GameState, action, size) -> GameState:
    state = state._replace(consecutive_pass_count=jnp.int32(0))
    xy = action
    num_captured_stones_before = state.num_captured_stones[state.color]

    ko_may_occur = _ko_may_occur(state, xy, size)

    # Remove killed stones
    adj_xy = _neighbour(xy, size)
    oppo_color = _opponent_color(state)
    chain_id = state.chain_id_board[adj_xy]
    num_pseudo, idx_sum, idx_squared_sum = _count(state, size)
    chain_ix = jnp.abs(chain_id) - 1
    is_atari = (idx_sum[chain_ix] ** 2) == idx_squared_sum[chain_ix] * num_pseudo[chain_ix]
    single_liberty = (idx_squared_sum[chain_ix] // idx_sum[chain_ix]) - 1
    is_killed = (adj_xy != -1) & (chain_id * oppo_color > 0) & is_atari & (single_liberty == xy)
    state = jax.lax.fori_loop(
        0,
        4,
        lambda i, s: jax.lax.cond(
            is_killed[i],
            lambda: _remove_stones(s, chain_id[i], adj_xy[i], ko_may_occur),
            lambda: s,
        ),
        state,
    )
    state = _set_stone(state, xy)

    # Merge neighbours
    state = jax.lax.fori_loop(0, 4, lambda i, s: _merge_around_xy(i, s, xy, size), state)

    # Check Ko
    state = jax.lax.cond(
        state.num_captured_stones[state.color] - num_captured_stones_before == 1,
        lambda: state,
        lambda: state._replace(ko=jnp.int32(-1)),
    )

    return state


def _merge_around_xy(i, state: GameState, xy, size):
    my_color = _my_color(state)
    adj_xy = _neighbour(xy, size)[i]
    is_off = adj_xy == -1
    is_my_chain = state.chain_id_board[adj_xy] * my_color > 0
    state = jax.lax.cond(
        ((~is_off) & is_my_chain),
        lambda: _merge_chain(state, xy, adj_xy),
        lambda: state,
    )
    return state


def _set_stone(state: GameState, xy) -> GameState:
    my_color = _my_color(state)
    return state._replace(
        chain_id_board=state.chain_id_board.at[xy].set((xy + 1) * my_color),
    )


def _merge_chain(state: GameState, xy, adj_xy):
    my_color = _my_color(state)
    new_id = jnp.abs(state.chain_id_board[xy])
    adj_chain_id = jnp.abs(state.chain_id_board[adj_xy])
    small_id = jnp.minimum(new_id, adj_chain_id) * my_color
    large_id = jnp.maximum(new_id, adj_chain_id) * my_color

    # Keep larger chain ID and connect to the chain with smaller ID
    chain_id_board = jnp.where(
        state.chain_id_board == large_id,
        small_id,
        state.chain_id_board,
    )

    return state._replace(chain_id_board=chain_id_board)


def _remove_stones(state: GameState, rm_chain_id, rm_stone_xy, ko_may_occur) -> GameState:
    surrounded_stones = state.chain_id_board == rm_chain_id
    num_captured_stones = jnp.count_nonzero(surrounded_stones)
    chain_id_board = jnp.where(surrounded_stones, 0, state.chain_id_board)
    ko = jax.lax.cond(
        ko_may_occur & (num_captured_stones == 1),
        lambda: jnp.int32(rm_stone_xy),
        lambda: state.ko,
    )
    return state._replace(
        chain_id_board=chain_id_board,
        num_captured_stones=state.num_captured_stones.at[state.color].add(num_captured_stones),
        ko=ko,
    )


def _count(state: GameState, size):
    ZERO = jnp.int32(0)
    chain_id_board = jnp.abs(state.chain_id_board)
    is_empty = chain_id_board == 0
    idx_sum = jnp.where(is_empty, jnp.arange(1, size**2 + 1), ZERO)
    idx_squared_sum = jnp.where(is_empty, jnp.arange(1, size**2 + 1) ** 2, ZERO)

    @jax.vmap
    def _count_neighbor(xy):
        neighbors = _neighbour(xy, size)
        on_board = neighbors != -1
        return (
            jnp.where(on_board, is_empty[neighbors], ZERO).sum(),
            jnp.where(on_board, idx_sum[neighbors], ZERO).sum(),
            jnp.where(on_board, idx_squared_sum[neighbors], ZERO).sum(),
        )

    idx = jnp.arange(size**2)
    num_pseudo, idx_sum, idx_squared_sum = _count_neighbor(idx)

    @jax.vmap
    def _num_pseudo(x):
        return jnp.where(chain_id_board == (x + 1), num_pseudo, ZERO).sum()

    @jax.vmap
    def _idx_sum(x):
        return jnp.where(chain_id_board == (x + 1), idx_sum, ZERO).sum()

    @jax.vmap
    def _idx_squared_sum(x):
        return jnp.where(chain_id_board == (x + 1), idx_squared_sum, ZERO).sum()

    return _num_pseudo(idx), _idx_sum(idx), _idx_squared_sum(idx)


def _my_color(state: GameState):
    return jnp.int32([1, -1])[state.color]


def _opponent_color(state: GameState):
    return jnp.int32([-1, 1])[state.color]


def _ko_may_occur(state: GameState, xy: int, size: int) -> Array:
    x = xy // size
    y = xy % size
    oob = jnp.bool_([x - 1 < 0, x + 1 >= size, y - 1 < 0, y + 1 >= size])
    oppo_color = _opponent_color(state)
    is_occupied_by_opp = state.chain_id_board[_neighbour(xy, size)] * oppo_color > 0
    return (oob | is_occupied_by_opp).all()


def _neighbour(xy, size):
    dx = jnp.int32([-1, +1, 0, 0])
    dy = jnp.int32([0, 0, -1, +1])
    xs = xy // size + dx
    ys = xy % size + dy
    on_board = (0 <= xs) & (xs < size) & (0 <= ys) & (ys < size)
    return jnp.where(on_board, xs * size + ys, -1)


def _neighbours(size):
    return jax.vmap(partial(_neighbour, size=size))(jnp.arange(size**2))


def _check_PSK(state: GameState):
    """On PSK implementations.

    Tromp-Taylor rule employ PSK. However, implementing strict PSK is inefficient because

    - Simulator has to store all previous board (or hash) history, and
    - Agent also has to remember all previous board to avoid losing by PSK

    As PSK rarely happens, as far as our best knowledge, it is usual to compromise in PSK implementations.
    For example,

    - OpenSpiel employs SSK (instead of PSK) for computing legal actions, and if PSK action happened, the game ends with tie.
      - Pros: Detect all PSK actions
      - Cons: Agent cannot know why the game ends with tie (if the same board is too old)
    - PettingZoo employs SSK for legal actions, and ignores even if PSK action happened.
      - Pros: Simple
      - Cons: PSK is totally ignored

    Note that the strict rule is "PSK for legal actions, and PSK action leads to immediate lose."
    So, we also compromise at this point, our approach is

    - Pgx employs SSK for legal actions, PSK is approximated by up to 8-steps before board, and approximate PSK action leads to immediate lose
      - Pros: Agent may be able to avoid PSK (as it observes board history up to 8-steps in AlphaGo Zero feature)
      - Cons: Ignoring the old same boards

    Anyway, we believe it's effect is very small as PSK rarely happens, especially in 19x19 board.
    """
    not_passed = state.consecutive_pass_count == 0
    is_psk = not_passed & (jnp.abs(state.board_history[0] - state.board_history[1:]).sum(axis=1) == 0).any()
    return is_psk


def _count_point(state: GameState, size):
    return jnp.array(
        [
            _count_ji(state, 1, size) + jnp.count_nonzero(state.chain_id_board > 0),
            _count_ji(state, -1, size) + jnp.count_nonzero(state.chain_id_board < 0),
        ],
        dtype=jnp.float32,
    )


def _count_ji(state: GameState, color: int, size: int):
    board = jnp.zeros_like(state.chain_id_board)
    board = jnp.where(state.chain_id_board * color > 0, 1, board)
    board = jnp.where(state.chain_id_board * color < 0, -1, board)
    # 0 = empty, 1 = mine, -1 = opponent's

    neighbours = _neighbours(size)

    def is_opp_neighbours(b):
        # True if empty and any of neighbours is opponent
        return (b == 0) & ((b[neighbours.flatten()] == -1).reshape(size**2, 4) & (neighbours != -1)).any(axis=1)

    def fill_opp(x):
        b, _ = x
        mask = is_opp_neighbours(b)
        return jnp.where(mask, -1, b), mask.any()

    b, _ = jax.lax.while_loop(lambda x: x[1], fill_opp, (board, True))

    return (b == 0).sum()
