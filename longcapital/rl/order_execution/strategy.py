import copy
from pathlib import Path

import torch.nn.functional as F  # noqa
from longcapital.rl.order_execution.interpreter import (
    TopkActionInterpreter,
    TopkDropoutSignalStrategyAction,
    TopkDropoutSignalStrategyActionInterpreter,
    TopkDropoutStrategyAction,
    TopkDropoutStrategyActionInterpreter,
    TradeStrategyStateInterpreter,
    WeightStrategyAction,
    WeightStrategyActionInterpreter,
)
from longcapital.rl.order_execution.policy import continuous, discrete
from longcapital.rl.order_execution.state import TradeStrategyState
from qlib.backtest.decision import TradeDecisionWO
from qlib.backtest.position import Position
from qlib.contrib.strategy import TopkDropoutStrategy as TopkDropoutStrategyBase
from qlib.contrib.strategy import WeightStrategyBase
from qlib.contrib.strategy.signal_strategy import BaseSignalStrategy
from tianshou.data import Batch


class TradeStrategy(BaseSignalStrategy):
    def action(self):
        state = TradeStrategyState(
            trade_executor=self.executor,
            trade_strategy=self,
            feature=self.get_feature(),
            trade_start_time=self.get_trade_start_end_time()[0],
            trade_end_time=self.get_trade_start_end_time()[1],
            pred_start_time=self.get_pred_start_end_time()[0],
            pred_end_time=self.get_pred_start_end_time()[1],
        )
        obs = [{"obs": self.state_interpreter.interpret(state), "info": {}}]
        policy_out = self.policy(Batch(obs))
        action = self.action_interpreter.interpret(state, policy_out.act)
        return action

    def get_feature(self, feature=None):
        if feature is None:
            pred_start_time, pred_end_time = self.get_pred_start_end_time()
            feature = self.signal.get_signal(
                start_time=pred_start_time, end_time=pred_end_time
            )

        stock_weight_dict = (
            self.executor.trade_account.current_position.get_stock_weight_dict(
                only_stock=False
            )
        )
        current_position_list = list(stock_weight_dict.keys())
        feature[("feature", "position")] = 0
        feature[("feature", "unhold")] = 1
        feature.loc[
            feature.index.isin(current_position_list), ("feature", "position")
        ] = 1
        feature.loc[
            feature.index.isin(current_position_list), ("feature", "unhold")
        ] = 0

        # sort to make sure the ranking distribution is similar across different dates
        feature.sort_values(
            by=[("feature", "position"), ("feature", "signal")],
            ascending=False,
            inplace=True,
        )

        return feature

    def get_trade_start_end_time(self):
        trade_step = self.trade_calendar.get_trade_step()
        return self.trade_calendar.get_step_time(trade_step)

    def get_pred_start_end_time(self):
        trade_step = self.trade_calendar.get_trade_step()
        return self.trade_calendar.get_step_time(trade_step, shift=1)


class TopkDropoutStrategy(TopkDropoutStrategyBase, TradeStrategy):
    def __init__(
        self,
        *,
        dim,
        stock_num,
        topk,
        n_drop=None,
        checkpoint_path=None,
        policy_cls=discrete.PPO,
        **kwargs,
    ):
        super().__init__(topk=topk, n_drop=n_drop, **kwargs)
        self.policy_cls = policy_cls
        self.n_drop_original = n_drop
        self.state_interpreter = TradeStrategyStateInterpreter(
            dim=dim, stock_num=stock_num
        )
        self.action_interpreter = TopkDropoutStrategyActionInterpreter(topk=topk)
        self.baseline_action_interpreter = TopkDropoutStrategyActionInterpreter(
            topk=topk, n_drop=n_drop, baseline=True
        )
        self.policy = policy_cls(
            obs_space=self.state_interpreter.observation_space,
            action_space=self.action_interpreter.action_space,
            weight_file=Path(checkpoint_path) if checkpoint_path else None,
        )
        if checkpoint_path:
            self.policy.eval()

    def __str__(self):
        return "TopkDropoutStrategy"

    def generate_trade_decision(
        self, execute_result=None, action: TopkDropoutStrategyAction = None
    ):
        if action is None:
            action = self.action()
        self.n_drop = action.n_drop
        return super().generate_trade_decision(execute_result)


class TopkDropoutSignalStrategy(TopkDropoutStrategyBase, TradeStrategy):
    def __init__(
        self,
        *,
        dim,
        stock_num,
        topk,
        n_drop,
        policy_cls=continuous.MetaTD3,
        checkpoint_path=None,
        **kwargs,
    ):
        super().__init__(topk=topk, n_drop=n_drop, **kwargs)
        self.policy_cls = policy_cls
        self.state_interpreter = TradeStrategyStateInterpreter(
            dim=dim, stock_num=stock_num
        )
        self.action_interpreter = TopkDropoutSignalStrategyActionInterpreter(
            stock_num=stock_num
        )
        self.baseline_action_interpreter = TopkDropoutSignalStrategyActionInterpreter(
            stock_num=stock_num, baseline=True
        )
        self.policy = policy_cls(
            obs_space=self.state_interpreter.observation_space,
            action_space=self.action_interpreter.action_space,
            weight_file=Path(checkpoint_path) if checkpoint_path else None,
        )
        if checkpoint_path:
            self.policy.eval()
        self.pred_score = None

    def __str__(self):
        return "TopkDropoutSignalStrategy"

    def get_pred_score(self):
        return self.pred_score

    def set_pred_score(self, action: TopkDropoutSignalStrategyAction):
        self.pred_score = action.signal

    def generate_trade_decision(
        self, execute_result=None, action: TopkDropoutSignalStrategyAction = None
    ):
        if action is None:
            action = self.action()
        self.set_pred_score(action)
        return super().generate_trade_decision(execute_result)


class WeightStrategy(WeightStrategyBase, TradeStrategy):
    def __init__(
        self,
        *,
        dim,
        stock_num,
        topk,
        checkpoint_path=None,
        policy_cls=continuous.MetaTD3,
        verbose=False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.verbose = verbose
        self.policy_cls = policy_cls

        self.state_interpreter = TradeStrategyStateInterpreter(
            dim=dim, stock_num=stock_num
        )
        self.action_interpreter = WeightStrategyActionInterpreter(
            stock_num=stock_num, topk=topk
        )
        self.baseline_action_interpreter = WeightStrategyActionInterpreter(
            stock_num=stock_num, topk=topk, baseline=True
        )
        self.policy = policy_cls(
            obs_space=self.state_interpreter.observation_space,
            action_space=self.action_interpreter.action_space,
            weight_file=Path(checkpoint_path) if checkpoint_path else None,
        )
        if checkpoint_path:
            self.policy.eval()

    def __str__(self):
        return "WeightStrategy"

    def generate_trade_decision(
        self, execute_result=None, action: WeightStrategyAction = None
    ):
        # generate_trade_decision
        # generate_target_weight_position() and generate_order_list_from_target_weight_position() to generate order_list

        # get the number of trading step finished, trade_step can be [0, 1, 2, ..., trade_len - 1]
        trade_step = self.trade_calendar.get_trade_step()
        trade_start_time, trade_end_time = self.trade_calendar.get_step_time(trade_step)
        pred_start_time, pred_end_time = self.trade_calendar.get_step_time(
            trade_step, shift=1
        )
        pred_score = self.signal.get_signal(
            start_time=pred_start_time, end_time=pred_end_time
        )
        if pred_score is None:
            return TradeDecisionWO([], self)
        current_temp = copy.deepcopy(self.trade_position)
        assert isinstance(current_temp, Position)  # Avoid InfPosition

        target_weight_position = self.generate_target_weight_position(
            score=pred_score,
            current=current_temp,
            trade_start_time=trade_start_time,
            trade_end_time=trade_end_time,
            action=action,
        )
        order_list = (
            self.order_generator.generate_order_list_from_target_weight_position(
                current=current_temp,
                trade_exchange=self.trade_exchange,
                risk_degree=self.get_risk_degree(trade_step),
                target_weight_position=target_weight_position,
                pred_start_time=pred_start_time,
                pred_end_time=pred_end_time,
                trade_start_time=trade_start_time,
                trade_end_time=trade_end_time,
            )
        )
        return TradeDecisionWO(order_list, self)

    def generate_target_weight_position(
        self, score, current, trade_start_time, trade_end_time, action=None
    ):
        if not action:
            action = self.action()
        return action.target_weight_position


class TopkStrategy(WeightStrategy):
    def __init__(
        self,
        *,
        dim,
        stock_num,
        checkpoint_path=None,
        policy_cls=discrete.MetaPPO,
        verbose=False,
        **kwargs,
    ):
        super(WeightStrategy, self).__init__(**kwargs)
        self.verbose = verbose
        self.policy_cls = policy_cls

        self.state_interpreter = TradeStrategyStateInterpreter(
            dim=dim, stock_num=stock_num
        )
        self.action_interpreter = TopkActionInterpreter(stock_num=stock_num)
        self.baseline_action_interpreter = TopkActionInterpreter(
            stock_num=stock_num, baseline=True
        )
        self.policy = policy_cls(
            obs_space=self.state_interpreter.observation_space,
            action_space=self.action_interpreter.action_space,
            weight_file=Path(checkpoint_path) if checkpoint_path else None,
        )
        if checkpoint_path:
            self.policy.eval()

    def __str__(self):
        return "TopkStrategy"
