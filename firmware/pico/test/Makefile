# Host-side tests for the protocol code: make -C firmware/pico/test

CFLAGS ?= -O1 -g -Wall -Wextra -fsanitize=address,undefined

test: test_irtoy
	./test_irtoy

test_irtoy: test_irtoy.c ../src/irtoy.c ../src/irtoy.h
	$(CC) $(CFLAGS) -I../src -o $@ test_irtoy.c ../src/irtoy.c

clean:
	rm -f test_irtoy

.PHONY: test clean
