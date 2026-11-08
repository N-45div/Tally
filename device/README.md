# Tally's desk device

A small box on the finance team's desk. When Tally holds an invoice, it says so and why ("it asks for payment to a
different bank account from the one on file; call them on the number on file, …"). Hold its button and ask about
the queue: what is held, what is ready, why one invoice is held. It can put an invoice on hold when told to. It
cannot approve, pay, reject or release anything; the server has no call for that, whatever is said to it.

Everything it says is written by Tally's server from Tally's records: vendor names and phone numbers on file, and
amounts. Nothing printed on an invoice is spoken, so an attacker's words on an invoice never reach the person at the
desk. Every question and every hold is in Tally's ledger.

## The hardware

An ESP32-S3 board with 16 MB of flash and 8 MB of PSRAM, an INMP441 microphone, a MAX98357A speaker amplifier, a
1.8" ST7735 screen and its BOOT button: the OceanLabz "AI Explorer ESP32-S3-CAM" board, as tested. The pins are in
[include/board.h](include/board.h); another board needs only that file changed.

## Build and flash

```bash
pip install platformio
cd device
pio run -t upload        # builds and flashes over USB; Tally's settings on the board (server, token, Wi-Fi) are kept
pio device monitor       # the serial console, 115200
```

Pinned: platform `espressif32@7.1.3` (Arduino core 2.0.17), Adafruit ST7735 1.11.0, Adafruit GFX 1.12.6,
ArduinoJson 7.4.3.

## Set it up

1. In Tally, open **Devices**, add a device with a name (it goes in the ledger), and copy the token it shows. The
   token is shown once; Tally keeps only its hash.
2. With the board on USB:

   ```bash
   py provision.py --wifi --server https://<your Tally host>
   ```

   It asks for the token at a hidden prompt, sends it to the board, and prints the board's status. The token stays
   in the board's own storage and is never printed. The Wi-Fi network must be 2.4 GHz. `--wifi` asks for its name
   and password (or takes `TALLY_WIFI_NAME` and `TALLY_WIFI_PASSWORD`); leave it out to keep the network already set.

### Without the host: Tally on a laptop

The device can be tried against a laptop on the same network. The desk worker needs only the model key, and the
invoices are written as readings, so no file is opened and no microVM is needed:

```bash
python -m lab.desk_local ~/tally-desk-local seed                 # records, a person, a device and its token
python -m lab.desk_local ~/tally-desk-local serve                # on port 8765, with VULTR_INFERENCE_API_KEY set
TALLY_DEVICE_TOKEN=$(cat ~/tally-desk-local/device-token.txt) py provision.py --server http://<laptop's address>:8765
python -m lab.desk_local ~/tally-desk-local add bank             # an invoice held for a new bank account: announced
```

On Windows, the firewall asks whether Python may take connections: allow it, on Public networks too if the Wi-Fi is
marked Public.

The screen shows the queue's counts (held, ready, waiting), the last thing said, and "Hold BOOT to ask" once it
reaches Tally. A green dot means the last call to Tally worked; yellow, it did not; red, no Wi-Fi.

## Use it

| | |
|---|---|
| Hold BOOT, speak, let go | Asks Tally. The answer is spoken and shown, with what was heard. Up to 12 seconds. |
| Tap BOOT | Says the last thing again. |

Things to ask: "How many invoices are held?", "What's on hold?", "Anything ready to approve?", "Why is invoice 12
held?", "Put invoice 12 on hold." A hold placed from the desk stays until a person releases it on the invoice's page
in Tally, with a note of why.

## The serial console

`status`, `server <url>`, `token <token>`, `wifi <name><TAB><password>`, `rotation <0-3>`, `beep`, `mic` (two
seconds of microphone levels), `ask <seconds>` (ask without the button), `news` (check now), `reboot`. Nothing secret
is printed.

## How it talks to Tally

HTTPS only, checked against the certificate authorities in [include/roots.h](include/roots.h) (ISRG Root X1, X2 and
Root YE, which the live host's Let's Encrypt chain needs). There is no insecure mode. A plain `http://` server is
accepted for testing on a local network only.

| Call | |
|---|---|
| `GET /device/hello` | at start: its name, the counts, and where announcements start (old ones are not replayed) |
| `GET /device/news?after=<id>` | every 8 seconds: new announcements and their audio |
| `POST /device/ask` | a recorded question, 16 kHz mono WAV |
| `GET /device/ask/<id>?wait=15` | the answer, then its audio |

## Limits

- From firmware 0.1.1 the board joins only the network set with `provision.py --wifi`, kept in Tally's own
  settings, and the firmware alone reconnects. With 0.1.0, after the Wi-Fi dropped once, the ESP32's own saved
  network crashed its Wi-Fi driver on every join (a LoadStoreError in `scan_build_chan_list`), so the board
  restarted in a loop. A board updated from 0.1.0 needs `provision.py --wifi` once.

- Push to talk only: no wake word.
- The microphone's output wanders slowly; the firmware filters out everything below about 50 Hz before sending.
  Measured on this board in a quiet room, that cut the level about twentyfold.
- An answer takes several seconds: speech to text, the question sorted into a fixed menu, the answer spoken (about
  3.5 seconds for 10 seconds of speech).
- Anyone who can reach the device can ask it questions and hold an invoice; it cannot approve or pay.
