// Carrier-modulated IR output through a PIO state machine.

#ifndef IR_TX_H
#define IR_TX_H

#include <stdint.h>

void ir_tx_init(unsigned pin);

// Start a transmission at the given carrier frequency.
void ir_tx_begin(uint32_t carrier_hz);

// Queue one mark and the space after it. Blocks while the FIFO is full.
void ir_tx_put(uint32_t mark_us, uint32_t space_us);

// Return once everything queued has been emitted.
void ir_tx_wait(void);

#endif
