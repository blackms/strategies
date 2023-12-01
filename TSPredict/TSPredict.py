# pragma pylint: disable=W0105, C0103, C0114, C0115, C0116, C0301, C0302, C0303, C0411, C0413,  W1203

"""
####################################################################################
TS_Predict - base class for 'simple' time series prediction
             Handles most of the logic for time series prediction. Subclasses should
             override the model-related functions

             Note that I use gain rather than price because it is a normalised value, and works better with prediction algorithms.
             I use the actual (future) gain to train a base model, which is then further refined for each individual pair.
             The model is created if it does not exist, and is trained on all available data before being saved.
             Models are saved in user_data/strategies/TSPredict/models/<class>/<class>.sav, where <class> is the name of the current class
             (TS_Predict if running this directly, or the name of the subclass). 
             If the model already exits, then it is just loaded and used.
             So, it makes sense to do initial training over a long period of time to create the base model. 
             If training, then no backtesting or tuning for individual pairs is performed (way faster).
             If you want to retrain (e.g. you changed indicators), then delete the model and run the strategy over a long time period

####################################################################################
"""


from datetime import datetime
from functools import reduce

import cProfile
import pstats
import traceback

import numpy as np
# Get rid of pandas warnings during backtesting
import pandas as pd
from pandas import DataFrame, Series

import pywt

# from sklearn.ensemble import GradientBoostingRegressor
from sklearn.preprocessing import RobustScaler


# import freqtrade.vendor.qtpylib.indicators as qtpylib

from freqtrade.strategy import (IStrategy, DecimalParameter, CategoricalParameter)
from freqtrade.persistence import Trade
import freqtrade.vendor.qtpylib.indicators as qtpylib

pd.options.mode.chained_assignment = None  # default='warn'

# Strategy specific imports, files must reside in same folder as strategy
import sys
from pathlib import Path

import os
import joblib
import copy

group_dir = str(Path(__file__).parent)
strat_dir = str(Path(__file__).parent.parent)
sys.path.append(strat_dir)
sys.path.append(group_dir)

# # this adds  ../utils
# sys.path.append("../utils")

import logging
import warnings

log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)
warnings.simplefilter(action='ignore', category=pd.errors.PerformanceWarning)
warnings.simplefilter(action='ignore', category=FutureWarning)

from xgboost import XGBRegressor
# from lightgbm import LGBMRegressor
from sklearn.linear_model import PassiveAggressiveRegressor
from sklearn.linear_model import SGDRegressor

from utils.DataframeUtils import DataframeUtils, ScalerType # pylint: disable=E0401
# import pywt
import talib.abstract as ta





class TSPredict(IStrategy):
    # Do *not* hyperopt for the roi and stoploss spaces


    plot_config = {
        'main_plot': {
            'close': {'color': 'cornflowerblue'}
        },
        'subplots': {
            "Diff": {
                'predicted_gain': {'color': 'orange'},
                'gain': {'color': 'lightblue'},
                'target_profit': {'color': 'lightgreen'},
                'target_loss': {'color': 'lightsalmon'}
            },
        }
    }


    # ROI table:
    minimal_roi = {
        "0": 0.06
    }

    # Stoploss:
    stoploss = -0.10

    # Trailing stop:
    trailing_stop = False
    trailing_stop_positive = None
    trailing_stop_positive_offset = 0.0
    trailing_only_offset_is_reached = False

    timeframe = '5m'
    inf_timeframe = '15m'

    use_custom_stoploss = True

    # Recommended
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = True

    # Required
    startup_candle_count: int = 128  # must be power of 2
    win_size = 14

    process_only_new_candles = True

    custom_trade_info = {} # pair-specific data
    curr_pair = ""

    ###################################

    # Strategy Specific Variable Storage

    ## Hyperopt Variables

    # model_window = startup_candle_count
    model_window = 128

    lookahead = 6

    df_coeffs: DataFrame = None
    coeff_table = None
    coeff_array = None
    gain_data = None

    training_data = None
    training_labels = None
    training_mode = False # do not set manually
    supports_incremental_training = True
    model_per_pair = False
    combine_models = True
    model_trained = False
    new_model = False

    norm_data = True
    # retrain_period = 12 # number of candles before retrining
    retrain_period = 2 # for testing only!

    dataframeUtils = None
    scaler = RobustScaler()
    model = None
    base_model = None

    curr_dataframe: DataFrame = None

    target_profit = 0.0
    target_loss = 0.0

    # hyperparams
 
    # enable entry/exit guards (safer vs profit)
    enable_entry_guards = CategoricalParameter(
        [True, False], default=True, space='buy', load=True, optimize=False)
    entry_guard_fwr = DecimalParameter(-1.0, 0.0, default=-0.0, decimals=1, space='buy', load=True, optimize=True)

    enable_exit_guards = CategoricalParameter(
        [True, False], default=True, space='sell', load=True, optimize=False)
    exit_guard_fwr = DecimalParameter(0.0, 1.0, default=0.0, decimals=1, space='sell', load=True, optimize=True)

    # use exit signal? 
    enable_exit_signal = CategoricalParameter([True, False], default=True, space='sell', load=True, optimize=True)

    # Custom Stoploss
    cstop_enable = CategoricalParameter([True, False], default=False, space='sell', load=True, optimize=True)
    cstop_start = DecimalParameter(0.0, 0.060, default=0.019, decimals=3, space='sell', load=True, optimize=True)
    cstop_ratio = DecimalParameter(0.7, 0.999, default=0.8, decimals=3, space='sell', load=True, optimize=True)

    # Custom Exit
    # profit threshold exit
    cexit_profit_threshold = DecimalParameter(0.005, 0.065, default=0.033, decimals=3, space='sell', load=True, optimize=True)
    cexit_use_profit_threshold = CategoricalParameter([True, False], default=True, space='sell', load=True, optimize=True)

    # loss threshold exit
    cexit_loss_threshold = DecimalParameter(-0.065, -0.005, default=-0.046, decimals=3, space='sell', load=True, optimize=True)
    cexit_use_loss_threshold = CategoricalParameter([True, False], default=False, space='sell', load=True, optimize=True)

    cexit_fwr_overbought = DecimalParameter(0.90, 1.00, default=0.98, decimals=2, space='sell', load=True, optimize=True)
    cexit_fwr_take_profit = DecimalParameter(0.90, 1.00, default=0.90, decimals=2, space='sell', load=True, optimize=True)
 

    ###################################

    def bot_start(self, **kwargs) -> None:

        if self.dataframeUtils is None:
            self.dataframeUtils = DataframeUtils()
            self.dataframeUtils.set_scaler_type(ScalerType.Robust)

        return

    ###################################

    """
    Informative Pair Definitions
    """

    def informative_pairs(self):
        return []

    ###################################

    """
    Indicator Definitions
    """

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        #NOTE: if you change the indicators, you need to regenerate the model

        # Base pair dataframe timeframe indicators
        curr_pair = metadata['pair']

        self.curr_dataframe = dataframe
        self.curr_pair = curr_pair

        # The following are needed for base functions, so do not remove.
        # Add custom indicators to add_strategy_indicators()

        # backward looking gain
        dataframe['gain'] = 100.0 * (dataframe['close'] - dataframe['close'].shift(self.lookahead)) / \
            dataframe['close'].shift(self.lookahead)
        dataframe['gain'].fillna(0.0, inplace=True)
        dataframe['gain'] = self.smooth(dataframe['gain'], 8)
        dataframe['gain'] = self.detrend_array(dataframe['gain'])

        # need to save the gain data for later scaling
        self.gain_data = dataframe['gain'].to_numpy().copy()

        # target profit/loss thresholds        
        dataframe['profit'] = dataframe['gain'].clip(lower=0.0)
        dataframe['loss'] = dataframe['gain'].clip(upper=0.0)
        win_size = self.lookahead
        n_std = 2.0
        dataframe['target_profit'] = dataframe['profit'].rolling(window=win_size).mean() + \
            n_std * dataframe['profit'].rolling(window=win_size).std()
        dataframe['target_loss'] = dataframe['loss'].rolling(window=win_size).mean() - \
            n_std * abs(dataframe['loss'].rolling(window=win_size).std())

        dataframe['target_profit'] = dataframe['target_profit'].clip(lower=0.1)
        dataframe['target_loss'] = dataframe['target_loss'].clip(upper=-0.1)
        
        dataframe['target_profit'] = np.nan_to_num(dataframe['target_profit'])
        dataframe['target_loss'] = np.nan_to_num(dataframe['target_loss'])

        # RSI
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=self.win_size)

        # Williams %R
        dataframe['wr'] = 0.02 * (self.williams_r(dataframe, period=self.win_size) + 50.0)

        # Fisher RSI
        rsi = 0.1 * (dataframe['rsi'] - 50)
        dataframe['fisher_rsi'] = (np.exp(2 * rsi) - 1) / (np.exp(2 * rsi) + 1)

        # Combined Fisher RSI and Williams %R
        dataframe['fisher_wr'] = (dataframe['wr'] + dataframe['fisher_rsi']) / 2.0

        # init prediction column
        dataframe['predicted_gain'] = 0.0


        # Add strategy-specific indicators
        dataframe = self.add_strategy_indicators(dataframe)


        # create and init the model, if first time (dataframe has to be populated first)
        if self.model is None:
            # print("    Loading model")
            self.load_model(np.shape(dataframe))

        # add the predictions
        # print("    Making predictions...")
        dataframe = self.add_predictions(dataframe)

        dataframe.fillna(0.0, inplace=True)

        # #DBG (cannot include this in 'real' strat because it's forward looking):
        # dataframe['dwt'] = self.get_dwt(dataframe['gain'])

        return dataframe
   
    ###################################

    def add_strategy_indicators(self, dataframe):

        # Override this function in subclasses and add extra indicators here

        return dataframe
  
    ###################################
    
    def smooth(self, y, window):
        box = np.ones(window)/window
        y_smooth = np.convolve(y, box, mode='same')
        y_smooth = np.round(y_smooth, decimals=3) #Hack: constrain to 3 decimal places (should be elsewhere, but convenient here)
        return np.nan_to_num(y_smooth)
    
    # look ahead to get future gain. Do *not* put this into the main dataframe!
    def get_future_gain(self, dataframe):

        df = self.convert_dataframe(dataframe)
        future_gain = df['gain'].shift(-self.lookahead).to_numpy()

        # future_gain = dataframe['gain'].shift(-self.lookahead).to_numpy()
        return self.smooth(future_gain, 8)
    
    ###################################

    # Williams %R
    def williams_r(self, dataframe: DataFrame, period: int = 14) -> Series:
        """Williams %R, or just %R, is a technical analysis oscillator showing the current closing price in relation to the high and low
            of the past N days (for a given N). It was developed by a publisher and promoter of trading materials, Larry Williams.
            Its purpose is to tell whether a stock or commodity market is trading near the high or the low, or somewhere in between,
            of its recent trading range.
            The oscillator is on a negative scale, from −100 (lowest) up to 0 (highest).
        """

        highest_high = dataframe["high"].rolling(center=False, window=period).max()
        lowest_low = dataframe["low"].rolling(center=False, window=period).min()

        WR = Series(
            (highest_high - dataframe["close"]) / (highest_high - lowest_low),
            name=f"{period} Williams %R",
        )

        return WR * -100


    ###################################


    #-------------

    def convert_dataframe(self, dataframe: DataFrame) -> DataFrame:
        df = dataframe.copy()

        # convert date column so that it can be scaled.
        if 'date' in df.columns:
            dates = pd.to_datetime(df['date'], utc=True)
            df['date'] = dates.astype('int64')

        df.fillna(0.0, inplace=True)

        df.set_index('date')
        df.reindex()

        if self.norm_data:
            # scale the dataframe
            self.scaler.fit(df)
            df = pd.DataFrame(self.scaler.transform(df), columns=df.columns)

        return df


    ###################################

    def detrend_array(self, a):
        '''
        if self.norm_data:
            # de-trend the data
            w_mean = a.mean()
            w_std = a.std()
            a_notrend = (a - w_mean) / w_std
            return a_notrend
        else:
            return a
        '''
        return a

    ###################################

    # Model-related funcs. Override in subclass to use a different type of model


    def get_model_path(self, pair):
        category = self.__class__.__name__
        root_dir = group_dir + "/models/" + category
        model_name = category
        if self.model_per_pair and (len(pair) > 0):
            model_name = model_name + "_" + pair.split("/")[0]
        path = root_dir + "/" + model_name + ".sav"
        return path
    
    def load_model(self, df_shape):

        model_path = self.get_model_path("")

        # load from file or create new model
        if os.path.exists(model_path):
            # use joblib to reload model state
            print("    loading from: ", model_path)
            self.model = joblib.load(model_path)
            self.model_trained = True
            self.new_model = False
            self.training_mode = False
        else:
            self.create_model(df_shape)
            self.model_trained = False
            self.new_model = True
            self.training_mode = True

        # sklearn family of regressors sometimes support starting with an existing model (warm_start), or incrementl training (partial_fit())
        if hasattr(self.model, 'warm_start'):
            self.model.warm_start = True
            self.supports_incremental_training = True # override default

        if hasattr(self.model, 'partial_fit'):
            self.supports_incremental_training = True # override default

        if self.model is None:
            print("***    ERR: model was not created properly ***")

        return

    #-------------

    def save_model(self):

        # save trained model (but only if didn't already exist)

        model_path = self.get_model_path("")

        # create directory if it doesn't already exist
        save_dir = os.path.dirname(model_path)
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)

        # use joblib to save model state
        print("    saving to: ", model_path)
        joblib.dump(self.model, model_path)

        return

    #-------------

    # override this method if you want a different type of prediction model
    def create_model(self, df_shape):

        # print("    creating new model using: XGBRegressor")
        params = {'n_estimators': 100, 'max_depth': 4, 'learning_rate': 0.1}
        self.model = XGBRegressor(**params)

        # # self.model = PassiveAggressiveRegressor(warm_start=True)
        # self.model = SGDRegressor(loss='huber')

        print(f"    creating new model using: {type(self.model)}")

        if self.model is None:
            print("***    ERR: create_model() - model was not created ***")
        return

    #-------------

    # train the model. Override if not an sklearn-compatible algorithm
    # set save_model=False if you don't want to save the model (needed for ML algorithms)
    def train_model(self, model, data: np.array, results: np.array, save_model):

        if self.model is None:
            print("***    ERR: no model ***")
            return

        x = np.nan_to_num(data)

        # print('    Updating existing model')
        if isinstance(model, XGBRegressor):
            # print('    Updating xgb_model')
            if self.new_model and (not self.model_trained):
                model.fit(x, results)
            else:
                model.fit(x, results, xgb_model=self.model)
        elif hasattr(model, "partial_fit"):
            # print('    partial_fit()')
            model.partial_fit(x, results)
        else:
            # print('    fit()')
            model.fit(x, results)

        return


    #-------------

    # single prediction (for use in rolling calculation)
    def predict(self, df) -> float:

        data = np.array(self.convert_dataframe(df))

        # y_pred = self.model.predict(data)[-1]
        y_pred = self.predict_data(self.model, data)[-1]

        return y_pred


    #-------------

    # get the data for this straegy. Override if necessary
    def get_data(self, dataframe):
        # default is to just normalise the dataframe and convert to numpy array
        df = np.array(self.convert_dataframe(dataframe))
        return df
 
    #-------------
    
    # initial training of the model
    def init_model(self, dataframe: DataFrame):

        # if model is not yet trained, or this is a new model and we want to combine across pairs, then train
        if (not self.model_trained) or (self.new_model and self.combine_models):

            df = dataframe

            future_gain_data = self.get_future_gain(df)
            data = self.get_data(df)

            training_data = data[:-self.lookahead-1].copy()
            training_labels = future_gain_data[:-self.lookahead-1].copy()

            if not self.model_trained:
                print(f'    initial training ({self.curr_pair})')
            else:
                print(f'    incremental training ({self.curr_pair})')

            self.train_model(self.model, training_data, training_labels, True)

            self.model_trained = True

            if self.new_model:
                self.save_model()
        
        # print(f'    model_trained:{self.model_trained} new_model:{self.new_model}  combine_models:{self.combine_models}')

        return
    
    #-------------
    
    # generate predictions for an np array (intended to be overriden if needed)
    def predict_data(self, model, data):
        x = np.nan_to_num(data)
        preds = model.predict(x)

        # de-norm
        scaler = RobustScaler()
        scaler.fit(self.gain_data.reshape(-1, 1))
        denorm_preds = scaler.inverse_transform(preds.reshape(-1, 1)).squeeze()

        denorm_preds = np.clip(denorm_preds, -3.0, 3.0)
        return denorm_preds


    #-------------
    
    # add predictions in a jumping fashion. This is a compromise - the rolling version is very slow
    # Note: you probably need to manually tune the parameters, since there is some limited lookahead here
    def add_jumping_predictions(self, dataframe: DataFrame) -> DataFrame:

        # limit training to what we would see in a live environment, otherwise results are too good
        live_buffer_size = 974
        df = dataframe

        # roll through the close data and predict for each step
        nrows = np.shape(df)[0]


        # build the coefficient table and merge into the dataframe (OK outside the main loop since it's built incrementally anyway)


        # set up training data
        future_gain_data = self.get_future_gain(df)
        data = self.get_data(dataframe)

        self.training_data = data.copy()
        self.training_labels = np.zeros(np.shape(future_gain_data), dtype=float)
        self.training_labels = future_gain_data.copy()

        # initialise the prediction array, using the close data
        pred_array = np.zeros(np.shape(future_gain_data), dtype=float)

        # win_size = 974
        win_size = 128

        # loop until we get to/past the end of the buffer
        start = win_size
        end = start + win_size
        train_end = start - 1
        train_size = 2 * win_size
        train_start = max(0, train_end - train_size)

        base_model = copy.deepcopy(self.model) # make a deep copy so that we don't override the baseline model

        while end < nrows:

            # extract the data and coefficients from the current window

            # (re-)train the model on prior data and get predictions

            if (not self.training_mode) and (self.supports_incremental_training):
                train_data = self.training_data[train_start:start-1].copy()
                train_results = self.training_labels[train_start:start-1].copy()
                base_model = copy.deepcopy(self.model)
                self.train_model(base_model, train_data, train_results, False)


            # rebuild data up to end of current window
            dslice = self.training_data[start:end].copy()
            preds = self.predict_data(base_model, dslice)

            # copy the predictions for this window into the main predictions array
            pred_array[start:end] = preds.copy()

            # move the window to the next segment
            end = end + win_size
            start = start + win_size
            train_end = start - 1
            train_start = max(0, train_end - train_size)

        # make sure the last section gets processed (the loop above may not exactly fit the data)
        # Note that we cannot use the last section for training because we don't have forward looking data

        '''
        if (not self.training_mode) and (self.supports_incremental_training):
            dslice = self.training_data[-(win_size+self.lookahead):-self.lookahead]
            cslice = self.training_labels[-(win_size+self.lookahead):-self.lookahead]
            self.train_model(base_model, dslice, cslice, False)
        '''

        # predict for last window
        dslice = self.training_data[-win_size:]
        # preds = self.model.predict(dslice)
        preds = self.predict_data(base_model, dslice)
        pred_array[-win_size:] = preds.copy()

        dataframe['predicted_gain'] = pred_array.copy()

        return dataframe

    #-------------
    
    # add the latest prediction, and update training periodically
    def add_latest_prediction(self, dataframe: DataFrame) -> DataFrame:

        df = dataframe

        try:
            # set up training data
            #TODO: see if we can do this incrementally instead of rebuilding every time, or just use portion of data
            future_gain_data = self.get_future_gain(df)
            data = self.get_data(dataframe)

            plen = len(self.custom_trade_info[self.curr_pair]['predictions'])
            dlen = len(dataframe['gain'])
            clen = min(plen, dlen)

            self.training_data = data[-clen:].copy()
            self.training_labels = future_gain_data[-clen:].copy()

            pred_array = np.zeros(clen, dtype=float)

            # print(f"[predictions]:{np.shape(self.custom_trade_info[self.curr_pair]['predictions'])}  pred_array:{np.shape(pred_array)}")

            # copy previous predictions and shift down by 1
            pred_array[-clen:] = self.custom_trade_info[self.curr_pair]['predictions'][-clen:].copy()
            pred_array = np.roll(pred_array, -1)
            pred_array[-1] = 0.0

            # cannot use last portion because we are looking ahead
            dslice = self.training_data[:-self.lookahead]
            tslice = self.training_labels[:-self.lookahead]

            # retrain base model and get predictions
            base_model = copy.deepcopy(self.model)
            self.train_model(base_model, dslice, tslice, False)
            preds = self.predict_data(base_model, self.training_data)

            # self.model = copy.deepcopy(base_model) # restore original model

            # only replace last prediction (i.e. don't overwrite the historical predictions)
            pred_array[-1] = preds[-1]

            dataframe['predicted_gain'] = 0.0
            dataframe['predicted_gain'][-clen:] = pred_array[-clen:].copy()
            self.custom_trade_info[self.curr_pair]['predictions'][-clen:] = pred_array[-clen:].copy()

            pg = preds[-1]
            if pg <= dataframe['target_loss'].iloc[-1]:
                tag = "(*)"
            elif pg >= dataframe['target_profit'].iloc[-1]:
                tag = " * "
            else:
                tag = "   "
            print(f'    {tag} predict {pg:6.2f}% gain for: {self.curr_pair}')

        except Exception as e:
            print("*** Exception in add_latest_prediction()")
            print(e) # prints the error message
            print(traceback.format_exc()) # prints the full traceback

        return dataframe

    
    #-------------
    
    # add predictions to dataframe['predicted_gain']
    def add_predictions(self, dataframe: DataFrame) -> DataFrame:

        # print(f"    {self.curr_pair} adding predictions")

        run_profiler = False

        if run_profiler:
            prof = cProfile.Profile()
            prof.enable()

        self.scaler = RobustScaler() # reset scaler each time

        self.init_model(dataframe)

        if self.curr_pair not in self.custom_trade_info:
            self.custom_trade_info[self.curr_pair] = {
                # 'model': self.model,
                'initialised': False,
                'predictions': None
            }


        if self.training_mode:
            print(f'    Training mode. Skipping backtest for {self.curr_pair}')
            dataframe['predicted_gain'] = 0.0
        else:
            if not self.custom_trade_info[self.curr_pair]['initialised']:
                print(f'    backtesting {self.curr_pair}')
                dataframe = self.add_jumping_predictions(dataframe)
                # dataframe = self.add_rolling_predictions(dataframe)
                self.custom_trade_info[self.curr_pair]['initialised'] = True
                self.custom_trade_info[self.curr_pair]['predictions'] = dataframe['predicted_gain'].copy()
            else:
                # print(f'    updating latest prediction for: {self.curr_pair}')
                dataframe = self.add_latest_prediction(dataframe)


        if run_profiler:
            prof.disable()
            # print profiling output
            stats = pstats.Stats(prof).strip_dirs().sort_stats("cumtime")
            stats.print_stats(20) # top 20 rows

        return dataframe
    
    ###################################

    """
    entry Signal
    """

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = []
        dataframe.loc[:, 'enter_tag'] = ''
       
        if self.training_mode:
            dataframe['enter_long'] = 0
            return dataframe
        
        if self.enable_entry_guards.value:
            # Fisher/Williams in oversold region
            conditions.append(dataframe['fisher_wr'] < self.entry_guard_fwr.value)

            # some trading volume
            conditions.append(dataframe['volume'] > 0)


        fwr_cond = (
            (dataframe['fisher_wr'] < -0.98)
        )


        # model triggers
        model_cond = (
            (
                # model predicts a rise above the entry threshold
                qtpylib.crossed_above(dataframe['predicted_gain'], dataframe['target_profit']) #&
                # (dataframe['predicted_gain'] >= dataframe['target_profit']) &
                # (dataframe['predicted_gain'].shift() >= dataframe['target_profit'].shift()) &

                # Fisher/Williams in oversold region
                # (dataframe['fisher_wr'] < -0.5)
            )
            # |
            # (
            #     # large gain predicted (ignore fisher_wr)
            #     qtpylib.crossed_above(dataframe['predicted_gain'], 2.0 * dataframe['target_profit']) 
            # )
        )
        
        

        # conditions.append(fwr_cond)
        conditions.append(model_cond)


        # set entry tags
        dataframe.loc[fwr_cond, 'enter_tag'] += 'fwr_entry '
        dataframe.loc[model_cond, 'enter_tag'] += 'model_entry '

        if conditions:
            dataframe.loc[reduce(lambda x, y: x & y, conditions), 'enter_long'] = 1

        return dataframe


    ###################################

    """
    exit Signal
    """

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = []
        dataframe.loc[:, 'exit_tag'] = ''

        if self.training_mode or (not self.enable_exit_signal.value):
            dataframe['exit_long'] = 0
            return dataframe

        # if self.enable_entry_guards.value:

        if self.enable_exit_guards.value:
            # Fisher/Williams in overbought region
            conditions.append(dataframe['fisher_wr'] > self.exit_guard_fwr.value)

            # some trading volume
            conditions.append(dataframe['volume'] > 0)

        # model triggers
        model_cond = (
            (

                 qtpylib.crossed_below(dataframe['predicted_gain'], dataframe['target_loss'] )
           )
        )

        conditions.append(model_cond)


        # set exit tags
        dataframe.loc[model_cond, 'exit_tag'] += 'model_exit '

        if conditions:
            dataframe.loc[reduce(lambda x, y: x & y, conditions), 'exit_long'] = 1

        return dataframe



    def confirm_trade_exit(self, pair: str, trade: Trade, order_type: str, amount: float,
                           rate: float, time_in_force: str, exit_reason: str,
                           current_time: datetime, **kwargs) -> bool:
                
        if self.dp.runmode.value not in ('backtest', 'plot', 'hyperopt'):
            print(f'Trade Exit: {pair}, rate: {round(rate, 4)}')

        return True
    
    ###################################

    """
    Custom Stoploss
    """

    # simplified version of custom trailing stoploss
    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                        current_profit: float, after_fill: bool, **kwargs) -> float:

        # if enable, use custom trailing ratio, else use default system
        if self.cstop_enable.value:
            # if current profit is above start value, then set stoploss at fraction of current profit
            if current_profit > self.cstop_start.value:
                return current_profit * self.cstop_ratio.value

        # return min(-0.001, max(stoploss_from_open(0.05, current_profit), -0.99))
        return self.stoploss


    ###################################

    """
    Custom Exit
    (Note that this runs even if use_custom_stoploss is False)
    """

    # simplified version of custom exit

    def custom_exit(self, pair: str, trade: Trade, current_time: 'datetime', current_rate: float,
                    current_profit: float, **kwargs):

        dataframe, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
        last_candle = dataframe.iloc[-1].squeeze()
        

        if not self.use_custom_stoploss:
            return None

        # strong sell signal, in profit
        if (current_profit > 0.001) and (last_candle['fisher_wr'] >= self.cexit_fwr_overbought.value):
            return 'fwr_overbought'

        # Above 1%, sell if Fisher/Williams in sell range
        if current_profit > 0.01:
            if last_candle['fisher_wr'] >= self.cexit_fwr_take_profit.value:
                return 'take_profit'
 

        # check profit against ROI target. This sort of emulates the freqtrade roi approach, but is much simpler
        if self.cexit_use_profit_threshold.value:
            if (current_profit >= self.cexit_profit_threshold.value):
                return 'cexit_profit_threshold'

        # check loss against threshold. This sort of emulates the freqtrade stoploss approach, but is much simpler
        if self.cexit_use_loss_threshold.value:
            if (current_profit <= self.cexit_loss_threshold.value):
                return 'cexit_loss_threshold'
              
        # Sell any positions if open for >= 1 day with any level of profit
        if ((current_time - trade.open_date_utc).days >= 1) & (current_profit > 0):
            return 'unclog_1'
        
        # Sell any positions at a loss if they are held for more than 7 days.
        if (current_time - trade.open_date_utc).days >= 7:
            return 'unclog_7'
        
        
        # big drop predicted. Should also trigger an exit signal, but this might be quicker (and will likely be 'market' sell)
        if (current_profit > 0) and (last_candle['predicted_gain'] <= last_candle['target_loss']):
            return 'predict_drop'
        

        # if in profit and exit signal is set, sell (even if exit signals are disabled)
        if (current_profit > 0) and (last_candle['exit_long'] > 0):
            return 'exit_signal'

        return None
    