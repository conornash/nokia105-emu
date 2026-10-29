# Builds the native helpers the emulator loads through ctypes.
ifeq ($(shell uname -s),Darwin)
EXT = dylib
else
EXT = so
endif
CC ?= cc

all: liblzma_sprd.$(EXT) libblockclock.$(EXT)

liblzma_sprd.$(EXT): lzma_sprd.c third_party/lzmadec.h
	$(CC) -O2 -shared -fPIC -Ithird_party -o $@ lzma_sprd.c

libblockclock.$(EXT): blockclock.c
	$(CC) -O2 -shared -fPIC -o $@ blockclock.c

# headless scenarios compared with a reference recorded from your dump
# (tests/regress.py); test-update records that reference
test: all
	.venv/bin/python -I tests/regress.py

test-update: all
	.venv/bin/python -I tests/regress.py --update

clean:
	rm -f liblzma_sprd.dylib liblzma_sprd.so libblockclock.dylib libblockclock.so

.PHONY: all clean test test-update
