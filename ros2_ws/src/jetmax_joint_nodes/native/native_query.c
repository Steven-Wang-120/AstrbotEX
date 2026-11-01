/* Position-query-only UART handoff. No caller-supplied packet/opcode exists. */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>
#include <linux/gpio.h>
#include <linux/serial.h>

struct jm_trace {
    uint64_t tx_on_ns, write_done_ns, tx_empty_ns, rx_on_ns;
    uint32_t bytes_written, polls;
    int32_t error, restore_error;
};

#ifdef JETMAX_QUERY_TEST
extern int jm_test_ioctl(int, unsigned long, void *);
extern ssize_t jm_test_write(int, const void *, size_t);
extern uint64_t jm_test_now(void);
#define SYS_IOCTL jm_test_ioctl
#define SYS_WRITE jm_test_write
#define NOW_NS jm_test_now
#else
#define SYS_IOCTL ioctl
#define SYS_WRITE write
static uint64_t jm_now(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) return 0;
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}
#define NOW_NS jm_now
#endif

static int set_pair(int fd, unsigned char rx, unsigned char tx) {
    struct gpiohandle_data data;
    memset(&data, 0, sizeof(data));
    data.values[0] = rx; data.values[1] = tx;
    return SYS_IOCTL(fd, GPIOHANDLE_SET_LINE_VALUES_IOCTL, &data);
}

/* echo_rx enables only the receive buffer while sending the fixed query.
 * The factory SDK also uses RX=1,TX=1 in portRest(). Direction output is
 * released to RX=1,TX=0 as soon as the hardware reports TX physically empty.
 * Replies are parsed and checksummed by the existing Python ServoClient.
 */
int jm_write_position_query(int uart_fd, int direction_fd, int servo_id,
                            int echo_rx, int timeout_us, struct jm_trace *trace) {
    unsigned char packet[6];
    uint64_t deadline, now;
    int error = 0, touched = 0;
    if (trace == NULL) return -EINVAL;
    memset(trace, 0, sizeof(*trace));
    if (uart_fd < 0 || direction_fd < 0 || servo_id < 1 || servo_id > 3 ||
        (echo_rx != 0 && echo_rx != 1) || timeout_us < 100 || timeout_us > 200000) {
        trace->error = EINVAL; return -EINVAL;
    }
    packet[0]=0x55;packet[1]=0x55;packet[2]=(unsigned char)servo_id;
    packet[3]=3;packet[4]=28;packet[5]=(unsigned char)(~(servo_id+3+28));
    now = NOW_NS();
    if (now == 0) { trace->error=EIO;return -EIO; }
    deadline=now+(uint64_t)timeout_us*1000ull;
    touched=1;
    if (set_pair(direction_fd, (unsigned char)echo_rx, 1) < 0) {
        error=errno;goto restore;
    }
    trace->tx_on_ns=NOW_NS();
    while (trace->bytes_written < sizeof(packet)) {
        ssize_t written;
        if (NOW_NS() >= deadline) { error=ETIMEDOUT;goto restore; }
        written=SYS_WRITE(uart_fd,packet+trace->bytes_written,sizeof(packet)-trace->bytes_written);
        if (written < 0) {
            if (errno==EINTR || errno==EAGAIN || errno==EWOULDBLOCK) continue;
            error=errno;goto restore;
        }
        if (written==0) continue;
        if ((size_t)written > sizeof(packet)-trace->bytes_written) { error=EIO;goto restore; }
        trace->bytes_written+=(uint32_t)written;
    }
    trace->write_done_ns=NOW_NS();
    for (;;) {
        int status=0, queued=0;
        if (NOW_NS() >= deadline) { error=ETIMEDOUT;goto restore; }
        if (SYS_IOCTL(uart_fd,TIOCOUTQ,&queued)<0) { error=errno;goto restore; }
        if (SYS_IOCTL(uart_fd,TIOCSERGETLSR,&status)<0) { error=errno;goto restore; }
        trace->polls++;
        if (queued==0 && (status&TIOCSER_TEMT)) break;
    }
    trace->tx_empty_ns=NOW_NS();
restore:
    if (touched) {
        if (set_pair(direction_fd,1,0)<0) {
            trace->restore_error=errno;
            if (!error) error=errno;
        } else trace->rx_on_ns=NOW_NS();
    }
    if (!error && NOW_NS()>deadline) error=ETIMEDOUT;
    trace->error=error;
    return error ? -error : 0;
}
