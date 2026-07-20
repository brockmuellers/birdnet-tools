"""
Cannot be run on the raspberry pi unless Pillow is installed
(generally not on BirdNET-Pi installations).
"""
import os

from PIL import Image, ImageOps

# Define your folders
INPUT_FOLDER = "source_photos"
OUTPUT_FOLDER = "bins"

if not os.path.exists(OUTPUT_FOLDER):
    os.makedirs(OUTPUT_FOLDER)

print("Starting conversion...")

for filename in os.listdir(INPUT_FOLDER):
    if filename.lower().endswith(('.png', '.jpg', '.jpeg')):
        input_path = os.path.join(INPUT_FOLDER, filename)
        output_filename = os.path.splitext(filename)[0] + '.bin'
        output_path = os.path.join(OUTPUT_FOLDER, output_filename)

        # 1. Open, resize, and crop perfectly to fit the 416x240 screen
        img = Image.open(input_path)
        img = ImageOps.fit(img, (416, 240))

        # 2. Convert to 1-bit Black & White (applies Floyd-Steinberg dithering automatically)
        img = img.convert('1')

        # 3. Translate pixels into raw bytes (8 pixels per byte)
        pixels = list(img.getdata())
        byte_array = bytearray()

        for i in range(0, len(pixels), 8):
            byte = 0
            for j in range(8):
                # Pillow uses 255 for white. We map white to 1 and black to 0.
                if pixels[i + j] == 255:
                    byte |= (1 << (7 - j))
            byte_array.append(byte)

        # 4. Save as a pure binary file
        with open(output_path, 'wb') as f:
            f.write(byte_array)

        print(f"Processed: {filename} -> {output_filename} ({len(byte_array)} bytes)")

print("Done! Transfer the 'bins' folder to your Raspberry Pi.")
