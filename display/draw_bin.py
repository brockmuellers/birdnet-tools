import os
import random
import time

import spidev
from gpiozero import InputDevice, OutputDevice

# ==========================================
# 1. PIN CONFIGURATION (BCM Numbers, NOT physical pin numbers)
# ==========================================
RST_PIN = 17   # Reset pin
DC_PIN = 25    # Data/Command pin
BUSY_PIN = 24  # Busy pin

rst = OutputDevice(RST_PIN, initial_value=True)
dc = OutputDevice(DC_PIN, initial_value=False)
busy = InputDevice(BUSY_PIN)

# ==========================================
# 2. SPI SETUP
# ==========================================
spi = spidev.SpiDev()
spi.open(0, 0) # Bus 0, Device 0
spi.max_speed_hz = 4000000 # 4 MHz is safe for e-paper
spi.mode = 0b00

def send_command(cmd):
    dc.off() # Low = Command mode
    # spidev handles the CS pin automatically here!
    spi.xfer2([cmd])

def send_data(data):
    dc.on()  # High = Data mode
    if isinstance(data, int):
        spi.xfer2([data])
    elif isinstance(data, list):
        # Linux SPI buffers are limited to 4096 bytes per burst.
        # We chunk the 12,480 byte array to prevent overflow errors.
        for i in range(0, len(data), 4096):
            spi.xfer2(data[i:i+4096])

def wait_until_idle():
    # WeAct screens usually pull the BUSY pin LOW (0) while drawing.
    # It goes HIGH (1) when ready for the next command.
    while busy.value == 0:
        time.sleep(0.05)

def init_display():
    # 1. Hardware Reset
    rst.off()
    time.sleep(0.1)
    rst.on()
    time.sleep(0.1)
    wait_until_idle()

    # 2. Standard UC8253 Initialization Sequence
    send_command(0x01) # Power setting
    send_data([0x03, 0x00, 0x2B, 0x2B])

    send_command(0x04) # Power ON
    wait_until_idle()

    send_command(0x00) # Panel setting
    send_data(0x0F)

    send_command(0x61) # Set Resolution (240x416; portrait)
    send_data([0xF0, 0x01, 0xA0])

def display_image(image_data):
    # 3. Push Image Data
    # Buffer 0x13 is strictly used for new image data on this controller
    send_command(0x13) # Data Start Transmission
    send_data(image_data)

    # For some reason, also need to do 0x10, otherwise the result is striped
    send_command(0x10) # Data Start Transmission
    send_data(image_data)

    # 4. Refresh Screen
    send_command(0x12) # Display Refresh
    wait_until_idle()

def sleep_display():
    # 5. Power Down (Crucial to prevent e-ink burn-in)
    send_command(0x02) # Power OFF
    wait_until_idle()
    send_command(0x07) # Deep Sleep
    send_data(0xA5)

# ==========================================
# 3. MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    BINS_DIR = "bins"

    # Gather all .bin files
    available_bins = [f for f in os.listdir(BINS_DIR) if f.endswith('.bin')]

    if not available_bins:
        print(f"Error: No .bin files found in '{BINS_DIR}'")
        exit()

    # Select a random bird photo
    chosen_bird = random.choice(available_bins)
    print(f"Waking screen to display: {chosen_bird}")

    # Read the raw byte file into a list of integers
    with open(os.path.join(BINS_DIR, chosen_bird), "rb") as f:
        image_data = list(f.read())

    init_display()
    display_image(image_data)
    sleep_display()
    print("Update complete! Display is asleep.")
