"""
Adds WASAPI auto-resample support so fixed-rate devices like Bluetooth
headsets (44.1/48 kHz only) can be opened at the rates this app streams at.
"""
import sys

P1 = "core/audio_devices.py"
with open(P1, "r", encoding="utf-8") as f:
    c1 = f.read()

if "extra_settings_for" in c1:
    print("audio_devices.py already patched.")
else:
    anchor = "def _usable(idx: int, kind: str) -> bool:"
    if c1.count(anchor) != 1:
        print("ERROR: anchor not found in audio_devices.py - no changes made.")
        sys.exit(1)

    helper = '''def _hostapi_name(idx) -> str:
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        apis = [a.get("name", "") for a in sd.query_hostapis()]
        hostapi_idx = devices[idx].get("hostapi", -1)
        if 0 <= hostapi_idx < len(apis):
            return apis[hostapi_idx].lower()
    except Exception:
        pass
    return ""


def extra_settings_for(idx):
    if idx is None:
        return None
    try:
        import sounddevice as sd
        if "wasapi" in _hostapi_name(idx):
            return sd.WasapiSettings(auto_convert=True)
    except Exception:
        pass
    return None


''' + anchor

    c1 = c1.replace(anchor, helper, 1)

    old_usable_body = """        if kind == "input":
            st = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                blocksize=1024, device=idx,
                                callback=lambda *_a: None)
        else:
            st = sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16",
                                    blocksize=1024, device=idx)"""
    new_usable_body = """        extra = extra_settings_for(idx)
        if kind == "input":
            st = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                blocksize=1024, device=idx,
                                callback=lambda *_a: None,
                                extra_settings=extra)
        else:
            st = sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16",
                                    blocksize=1024, device=idx,
                                    extra_settings=extra)"""
    if old_usable_body not in c1:
        print("ERROR: _usable body not found - no changes made.")
        sys.exit(1)
    c1 = c1.replace(old_usable_body, new_usable_body, 1)

    old_tw_out = """            st = sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16",
                                    blocksize=1024, device=idx)
            st.start()
            t0 = time.monotonic()"""
    new_tw_out = """            st = sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16",
                                    blocksize=1024, device=idx,
                                    extra_settings=extra_settings_for(idx))
            st.start()
            t0 = time.monotonic()"""
    if old_tw_out not in c1:
        print("ERROR: _transport_works output body not found - no changes made.")
        sys.exit(1)
    c1 = c1.replace(old_tw_out, new_tw_out, 1)

    old_tw_in = """            st = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                blocksize=1024, device=idx, callback=_cb)"""
    new_tw_in = """            st = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                blocksize=1024, device=idx, callback=_cb,
                                extra_settings=extra_settings_for(idx))"""
    if old_tw_in not in c1:
        print("ERROR: _transport_works input body not found - no changes made.")
        sys.exit(1)
    c1 = c1.replace(old_tw_in, new_tw_in, 1)

    old_pref = "_PREFERRED_APIS = {\r\n    \"Windows\": (\"directsound\", \"mme\", \"wasapi\"),"
    new_pref = "_PREFERRED_APIS = {\r\n    \"Windows\": (\"wasapi\", \"directsound\", \"mme\"),"
    if old_pref not in c1:
        old_pref2 = "_PREFERRED_APIS = {\n    \"Windows\": (\"directsound\", \"mme\", \"wasapi\"),"
        new_pref2 = "_PREFERRED_APIS = {\n    \"Windows\": (\"wasapi\", \"directsound\", \"mme\"),"
        if old_pref2 not in c1:
            print("ERROR: _PREFERRED_APIS line not found - no changes made.")
            sys.exit(1)
        c1 = c1.replace(old_pref2, new_pref2, 1)
    else:
        c1 = c1.replace(old_pref, new_pref, 1)

    with open(P1, "w", encoding="utf-8") as f:
        f.write(c1)
    print("Patched core/audio_devices.py")

P2 = "main.py"
with open(P2, "r", encoding="utf-8") as f:
    c2 = f.read()

if "extra_settings=audio_devices.extra_settings_for" in c2:
    print("main.py already patched.")
else:
    old_mic_open = """            def _open_mic(dev):
                return sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    device=dev,
                    callback=callback,
                )"""
    new_mic_open = """            def _open_mic(dev):
                return sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    device=dev,
                    callback=callback,
                    extra_settings=audio_devices.extra_settings_for(dev),
                )"""
    if old_mic_open not in c2:
        print("ERROR: _open_mic body not found - no changes made to main.py.")
        sys.exit(1)
    c2 = c2.replace(old_mic_open, new_mic_open, 1)

    old_spk_open = """        def _open_spk(dev):
            st = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=dev,
            )
            st.start()
            return st"""
    new_spk_open = """        def _open_spk(dev):
            st = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=dev,
                extra_settings=audio_devices.extra_settings_for(dev),
            )
            st.start()
            return st"""
    if old_spk_open not in c2:
        print("ERROR: _open_spk body not found - no changes made to main.py.")
        sys.exit(1)
    c2 = c2.replace(old_spk_open, new_spk_open, 1)

    with open(P2, "w", encoding="utf-8") as f:
        f.write(c2)
    print("Patched main.py")

print("All done. Run: python main.py")
