# Builds the LZMA helper the emulator loads through ctypes.
ifeq ($(shell uname -s),Darwin)
LIB = liblzma_sprd.dylib
else
LIB = liblzma_sprd.so
endif
CC ?= cc

all: $(LIB)

$(LIB): lzma_sprd.c third_party/lzmadec.h
	$(CC) -O2 -shared -fPIC -Ithird_party -o $@ lzma_sprd.c

clean:
	rm -f liblzma_sprd.dylib liblzma_sprd.so

.PHONY: all clean
