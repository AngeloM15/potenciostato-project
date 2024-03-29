import json
import logging
import time
from datetime import datetime
from re import T

# ADC libraries
import adafruit_ads1x15.ads1115 as ADS

# DAC libraries
import adafruit_mcp4725
import board
import busio
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns
from adafruit_ads1x15.ads1x15 import Mode
from adafruit_ads1x15.analog_in import AnalogIn

from tools import data
from tools.config import Potenciostato, Square, Triangular

module_logger = logging.getLogger("main.libdata")
log = logging.getLogger("main.libdata.Libdata")


class Libdata:
    def __init__(self):
        self.total_file_name = None
        self.temporal_file_name = None

    def save_json(self, data):
        """
        Set data in JSON format
        """
        json_data = {
            "device": "RodStat-bb663b",
            "timestamp": data[0],
            "sensors": {"DAC": data[1], "ADC": data[2]},
        }
        self.json_data = json_data
        log.debug(f"Data: {self.json_data}")

    def save_data(self, filename):
        """
        Append data in the last line of the file
        """
        file = open(filename, "a")
        # file.write("{}\n".format(self.json_data))
        json.dump(self.json_data, file)
        file.write("\n")
        file.close()

    def clear_data(self, filename):
        open(filename, "w").close()

    def load_data(self, filename):

        # Read lines in JSON format
        df = pd.read_json(filename, orient="records", lines=True)
        # Separate sensors column for each key in dictionary
        df = pd.concat(
            [df.drop(["sensors"], axis=1), pd.json_normalize(df["sensors"])], axis=1
        )
        # Define datetime
        df["DateTime"] = pd.to_datetime(df["timestamp"], format="%Y-%m-%d %H:%M:%S:%f")
        # Datetime as index
        df = df.set_index("DateTime").drop(
            [
                "timestamp",
            ],
            axis=1,
        )

        self.signal_df = df

    def filter_data(self, df, filter_factor):

        # Eliminante points that aren't in states change
        df = df.reset_index()
        df_to_filter = pd.DataFrame(columns=df.columns)

        for i, row in df.iterrows():

            if i > 0:
                if row["DAC"] != df["DAC"].iloc[i - 1]:
                    df_to_filter = pd.concat([row.to_frame().T, df_to_filter])
            elif i == 0:
                df_to_filter = pd.concat([df_to_filter, row.to_frame().T])

        df["DateTime"] = pd.to_datetime(df["DateTime"], format="%Y-%m-%d %H:%M:%S:%f")
        df_to_filter = df_to_filter.set_index("DateTime").sort_index()

        # Get al positive peaks
        df_pos = df_to_filter.loc[df_to_filter["ADC"] > 0]
        log.info(f"positive filter table:\n{df_pos}")

        # Get al negative peaks
        df_neg = df_to_filter.loc[df_to_filter["ADC"] < 0]
        # Add firt positive value
        first_row = df_to_filter.iloc[0, :].to_frame().T
        df_neg = pd.concat([first_row, df_neg])
        log.info(f"negative filter table:\n{df_neg}")

        # Set dataframe to plot ADC vs DAC
        df_inter = df_to_filter.copy()
        df_inter["up_env"] = df_pos["ADC"].astype(float)
        df_inter["down_env"] = df_neg["ADC"].astype(float)

        df_inter.drop(["ADC", "device"], axis=1, inplace=True)
        df_inter.reset_index(drop=True, inplace=True)
        df_inter.sort_values(by=["DAC"], ascending=False, inplace=True)
        # Interpolate
        df_inter.interpolate(inplace=True)
        # Smooth
        df_inter = df_inter.ewm(com=filter_factor).mean()

        df_inter["total"] = df_inter["down_env"] - df_inter["up_env"]
        df_inter = df_inter.melt(id_vars=["DAC"])
        log.info(f"filter table w interpolation:\n{df_inter}")

        return df_inter, df_pos, df_neg

    def plot_data(self, type_wave, filter_factor):

        df = self.signal_df

        # Plot data
        sns.set(style="darkgrid", context="paper", rc={"figure.figsize": (10, 8)})
        fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True)

        if type_wave == "triangular":
            sns.lineplot(data=df, x=df.index, y="DAC", ax=ax1)
            sns.lineplot(data=df, x=df.index, y="ADC", ax=ax2)
            plt.tight_layout()
            plt.show()

            # Smooth
            df = df.ewm(com=filter_factor).mean()
            g = sns.lineplot(
                data=df, x="DAC", y="ADC", sort=False, lw=1, estimator=None
            )
            plt.xlabel("Potencial (V)")
            plt.ylabel("Corriente (uA)")
            plt.tight_layout()
            plt.show()

        if type_wave == "square":

            df_interpolate, df_positive, df_negative = self.filter_data(
                df, filter_factor
            )

            sns.lineplot(data=df, x=df.index, y="DAC", ax=ax1)
            sns.lineplot(data=df, x=df.index, y="ADC", ax=ax2)
            sns.lineplot(data=df_positive, x=df_positive.index, y="ADC", ax=ax2)
            sns.lineplot(data=df_negative, x=df_negative.index, y="ADC", ax=ax2)
            plt.tight_layout()
            plt.show()

            sns.lineplot(
                data=df_interpolate,
                x="DAC",
                y="value",
                hue="variable",
                sort=False,
                lw=1,
                estimator=None,
            )
            plt.xlabel("Potencial (V)")
            plt.ylabel("Corriente (uA)")
            plt.tight_layout()
            plt.show()


class Libconversor:
    def __init__(self):
        self.data = data.Data()

        # Initialize I2C bus.
        i2c = busio.I2C(board.SCL, board.SDA)

        # Set DAC
        self.dac = adafruit_mcp4725.MCP4725(i2c)
        # amp = adafruit_max9744.MAX9744(self.i2c, address=0x60)

        # Set ADC
        self.ads = ADS.ADS1115(i2c)
        # Create single-ended input on channel 0
        self.chan0 = AnalogIn(self.ads, ADS.P0)

        # ADC Configuration
        if Potenciostato.signal == "triangular":
            self.ads.mode = Mode.CONTINUOUS
        elif Potenciostato.signal == "square":
            self.ads.mode = Mode.SINGLE
            self.ads.data_rate = 860

    def send_dac(self, data):

        data_rescaled = (5 / 3) * (1.58 - data)
        self.dac.normalized_value = data_rescaled / 5.2535
        # log.info(f"Send to DAC ---> {data}V / {data_rescaled}V / {data_rescaled/5.2535}")

    def get_adc(self):
        vol_in = self.chan0.voltage

        # Diodo equation
        volt_in_fixed = 0.1647 * vol_in * vol_in + 0.7305 * vol_in + 0.0544
        amp_in = (volt_in_fixed - 1.71) * 1000 / (8.2) - 2.1

        log.debug(f"voltage: {round(volt_in_fixed,2)}V / current: {round(amp_in,2)}A")
        return round(amp_in, 2)

    def process_data(self, dac_value, time_to_wait):

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S:%f")[:-3]

        # Send and receive signal
        self.send_dac(dac_value)
        adc_value = self.get_adc()

        self.data.save_json([ts, dac_value, adc_value])
        self.data.save_data(self.total_file_name)
        self.data.save_data(self.temporal_file_name)

        # time_to_wait = self.step/self.scan_rate
        time.sleep(time_to_wait)

    def triangular_wave(self):
        """Triangular curve generation."""
        initial_value = Triangular.init
        step = Triangular.steps
        count = 1
        n_loop = 0
        up = True
        down = False
        max_loop = Triangular.loops
        max_value = Triangular.max
        min_value = Triangular.min
        p_sample = step / Triangular.scan_rate

        value = round(initial_value, 2)
        self.process_data(value, p_sample)

        while True:
            if n_loop == max_loop:
                break

            if (count == 3) and (value == initial_value):
                count = 1
                n_loop += 1
                log.info(f"Loop number {n_loop}...")
                # print(f"loop number {n_loop}")
            elif up:
                value = round(value + step, 2)
                if value <= max_value:
                    self.process_data(value, p_sample)
                else:
                    value -= step
                    up = False
                    down = True
                    count += 1
                    # print(count)

            elif down:
                value = round(value - step, 2)
                if min_value <= value:
                    self.process_data(value, p_sample)
                else:
                    value += step
                    up = True
                    down = False
                    count += 1
                    # print(count)

    def square_wave(self):

        duty = Square.duty_cycle

        n_loop = 0
        step = 0
        freq = Square.freq_signal
        freq_sample = Square.freq_sample

        up = True
        down = False
        amplitude = Square.amp_signal
        initial_value = Square.initial
        final_value = Square.final
        counter = 0
        point_per_loop = freq_sample / freq

        log.info(f"Points per loop: {int(point_per_loop)}")

        while True:
            if (-amplitude - step) <= final_value:
                break

            if up:
                self.process_data(initial_value - step, 1 / freq_sample)
                counter += 1
                if counter == int(point_per_loop / 2):
                    up = False
                    down = True
                    counter = 0

            elif down:
                self.process_data(-amplitude - step, 1 / freq_sample)
                counter += 1
                if counter == point_per_loop - int(point_per_loop / 2):
                    up = True
                    down = False
                    counter = 0
                    step += self.offset
                    n_loop += 1
                    log.info(f"Loop number {n_loop}...")
