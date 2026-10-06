PYTHON ?= python3
EIM ?= eim
PORT ?= <USB_DEVICE>

.PHONY: build build-podman build-wslc test flash-uart jtag-probe flash-jtag smoke
build:
	$(PYTHON) scripts/fw.py eim
build-podman:
	$(PYTHON) scripts/fw.py podman
build-wslc:
	$(PYTHON) scripts/fw.py wslc
test:
	uv run python -m unittest discover -s tests -v
flash-uart:
	$(EIM) run 'sh -c "cd build/eim && python -m esptool --chip esp32c5 -p $(PORT) --before default-reset --after hard-reset write-flash @flash_args"' v6.0.3
jtag-probe:
	$(EIM) run 'openocd -f board/esp32c5-builtin.cfg -c "adapter serial <DEVICE_ID>; init; targets; shutdown"' v6.0.3
flash-jtag:
	$(EIM) run 'openocd -f board/esp32c5-builtin.cfg -c "adapter speed 1000; adapter serial <DEVICE_ID>; init; program_esp_bins build/eim flasher_args.json verify reset exit"' v6.0.3
smoke:
	uv run python scripts/monitor_smoke.py --port "$(PORT)"
