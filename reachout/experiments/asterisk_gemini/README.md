# Asterisk + Gemini Live test (Exotel alternative)

Goal: see whether a real phone call can talk to Gemini Live using a Bluetooth-paired phone as the
telephony gateway. Throwaway experiment; nothing here is wired into Reachout.

```
phone call <-> your phone <-Bluetooth HFP-> Asterisk (chan_mobile) -AudioSocket TCP-> bridge.py <-> Gemini Live
```

## Step 1: check Gemini alone (works on Windows, no Asterisk)

```bash
pip install -r requirements.txt sounddevice numpy
set GEMINI_API_KEY=...          # PowerShell: $env:GEMINI_API_KEY="..."
python bridge.py --local        # use headphones, otherwise it hears itself
```

`GEMINI_LIVE_MODEL` overrides the model (default `gemini-3.8-live`; names change often).

Or use the browser UI: `python web_ui.py`, open http://localhost:5000, click Start (mic + live transcript).

## Step 2: Asterisk over Bluetooth (needs Linux with a Bluetooth adapter)

Windows can't do this natively, and WSL has no Bluetooth passthrough by default. Use a Linux box,
Raspberry Pi or VM with a USB Bluetooth dongle passed through.

1. Install Asterisk with `chan_mobile` and `app_audiosocket` (check `asterisk -rx "module show like mobile"`).
   chan_mobile has been deprecated upstream; if your Asterisk version no longer ships it, use Asterisk 20
   or build the module separately.
2. Pair the phone with the Linux box (`bluetoothctl`), enable "phone calls" for that device.
3. Copy `asterisk/mobile.conf` and the dialplan in `asterisk/extensions.conf`, fill in the addresses,
   then `asterisk -rx "module reload"` and confirm `asterisk -rx "mobile show devices"` shows Connected.
4. Run `python bridge.py` on the same box (or point the AudioSocket line at its IP).
5. Dial out: `asterisk -rx "channel originate Mobile/phone/+91XXXXXXXXXX extension s@gemini-test"`
   or call the phone from another number to test inbound.

## What to expect / known risks

- Audio is 8 kHz slin both ways; Bluetooth HFP voice is narrowband, so Gemini's recognition may be a bit worse than step 1.
- Latency = Bluetooth + network + Gemini. Judge it by ear.
- One call at a time (one phone). This proves the voice loop, not Exotel-scale outbound.
- Python must be <= 3.12 (`audioop`). Asterisk AudioSocket frame details and Gemini model/config names were
  written from memory and the docs; fix them in `bridge.py` if a call fails.
- Calling real people from a personal SIM for campaigns may breach carrier terms and Indian telemarketing
  rules; use this for testing with your own numbers only.
