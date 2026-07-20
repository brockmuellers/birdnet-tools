"""
Cannot be run on the raspberry pi unless Pillow is installed
(generally not on BirdNET-Pi installations).
"""
import os

from PIL import Image, ImageOps

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

        # 1. Fit to Landscape
        img = Image.open(input_path)
        img = ImageOps.fit(img, (416, 240))

        # 2. ROTATE to match the hardware's native Portrait memory map
        img = img.transpose(Image.Transpose.ROTATE_270)

        # 3. Convert to 1-bit Black & White
        img = img.convert('1')

        # 4. Translate pixels into raw bytes
        pixels = list(img.getdata())
        byte_array = bytearray()

        for i in range(0, len(pixels), 8):
            byte = 0
            for j in range(8):
                if pixels[i + j] == 255:
                    byte |= (1 << (7 - j))
            byte_array.append(byte)

        with open(output_path, 'wb') as f:
            f.write(byte_array)

        print(f"Processed: {filename} -> {output_filename}")

print("Done! Transfer the new 'bird_bins' folder to your Raspberry Pi.")
