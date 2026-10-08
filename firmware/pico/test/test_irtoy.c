// Drives irtoy.c through the same command sequences as the kernel's ir_toy
// driver and checks the replies it depends on.

#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "irtoy.h"

static uint8_t replies[64][64];
static size_t reply_len[64];
static size_t nreplies;

static uint16_t sent[IRTOY_MAX_DURATIONS];
static size_t nsent;
static uint32_t sent_carrier;
static int transmits;

static void send(void *ctx, const uint8_t *buf, size_t len)
{
    (void)ctx;
    assert(nreplies < 64 && len <= 64);
    memcpy(replies[nreplies], buf, len);
    reply_len[nreplies++] = len;
}

static void transmit(void *ctx, const uint16_t *units, size_t count, uint32_t carrier_hz)
{
    (void)ctx;
    memcpy(sent, units, count * sizeof *units);
    nsent = count;
    sent_carrier = carrier_hz;
    transmits++;
}

static const irtoy_io_t io = { .send = send, .transmit = transmit };
static irtoy_t toy;

static void reset_log(void)
{
    nreplies = 0;
    nsent = 0;
    transmits = 0;
}

#define FEED(...)                                     \
    do {                                              \
        const uint8_t b[] = { __VA_ARGS__ };          \
        irtoy_feed(&toy, b, sizeof b);                \
    } while (0)

static void expect_reply(size_t i, const char *data, size_t len)
{
    assert(i < nreplies);
    assert(reply_len[i] == len);
    assert(memcmp(replies[i], data, len) == 0);
}

// irtoy_setup(): reset, version, sample mode.
static void setup(void)
{
    reset_log();
    FEED(0xff, 0xff, 0, 0, 0, 0, 0);
    assert(nreplies == 0);
    FEED('v');
    expect_reply(0, "V222", 4);
    FEED('s');
    expect_reply(1, "S01", 3);
    assert(nreplies == 2);
}

// irtoy_tx(): durations in us, converted and fed back as the driver does.
static void tx(const unsigned *us, size_t count)
{
    uint8_t buf[2 * (IRTOY_MAX_DURATIONS + 1)];
    size_t size = 0;
    for (size_t i = 0; i < count; i++) {
        unsigned v = (us[i] + IRTOY_UNIT_US / 2) / IRTOY_UNIT_US;
        if (!v)
            v = 1;
        buf[size++] = v >> 8;
        buf[size++] = v & 0xff;
    }
    buf[size++] = 0xff;
    buf[size++] = 0xff;

    reset_log();
    FEED(0);
    FEED('s');
    expect_reply(0, "S01", 3);
    FEED(0x26, 0x24, 0x25, 0x03);

    size_t off = 0, r = 1;
    while (off < size) {
        assert(r < nreplies && reply_len[r] == 1);
        size_t space = replies[r][0];
        assert(space > 0 && space <= 64);
        size_t n = size - off < space ? size - off : space;
        irtoy_feed(&toy, buf + off, n);
        off += n;
        r++;
    }

    // Count of bytes received, then success, as separate packets.
    assert(nreplies == r + 2);
    assert(reply_len[r] == 3 && replies[r][0] == 't');
    assert((size_t)(replies[r][1] << 8 | replies[r][2]) == size);
    expect_reply(r + 1, "C", 1);
}

int main(void)
{
    irtoy_init(&toy, &io);
    setup();

    // Default carrier ~38 kHz.
    unsigned nec_start[] = { 9000, 4500, 560 };
    tx(nec_start, 3);
    assert(transmits == 1 && nsent == 3);
    assert(sent[0] == 429 && sent[1] == 214 && sent[2] == 27);
    assert(sent_carrier == 37974);

    // irtoy_tx_carrier(36000): PR2 = round(48e6 / (16 * 36000)) - 1 = 82.
    reset_log();
    FEED(0x06, 82, 0);
    assert(nreplies == 0);
    tx(nec_start, 3);
    assert(sent_carrier == 36144);

    // Long enough to need many handshakes: the kernel's 1023-entry maximum
    // (LIRCBUF_SIZE is 1024 and the count must be odd).
    static unsigned many[1023];
    for (size_t i = 0; i < 1023; i++)
        many[i] = 210 + 21 * (i % 50);
    tx(many, 1023);
    assert(nsent == 1023);
    for (size_t i = 0; i < 1023; i++)
        assert(sent[i] == 10 + i % 50);

    // The driver re-runs setup after an error; it must work mid-session.
    setup();
    tx(nec_start, 3);
    assert(transmits == 1);

    puts("ok");
    return 0;
}
