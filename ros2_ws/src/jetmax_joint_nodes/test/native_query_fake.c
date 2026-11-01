#include <errno.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include <linux/gpio.h>
#include <linux/serial.h>
static uint64_t now;
static int mode, writes, transitions, states[8][2], limit;
static unsigned char bytes[32];static int count;
void jm_test_reset(int m,int l) { now=1000000;mode=m;writes=0;transitions=0;count=0;limit=l;memset(bytes,0,sizeof(bytes)); }
uint64_t jm_test_now(void) { now+=10000;return now; }
int jm_test_ioctl(int fd,unsigned long request,void *data) {
    (void)fd;
    if (request==GPIOHANDLE_SET_LINE_VALUES_IOCTL) {
        struct gpiohandle_data *d=data;
        if (transitions<8) { states[transitions][0]=d->values[0];states[transitions][1]=d->values[1]; }
        transitions++;return 0;
    }
    if (request==TIOCOUTQ) { *(int*)data=0;return 0; }
    if (request==TIOCSERGETLSR) { *(int*)data=mode==1?0:TIOCSER_TEMT;return 0; }
    errno=ENOTTY;return -1;
}
ssize_t jm_test_write(int fd,const void *data,size_t size) {
    (void)fd;writes++;
    if (mode==2) {errno=EIO;return -1;}
    if (limit>0 && size>(size_t)limit) size=(size_t)limit;
    if (count+(int)size>32) {errno=EIO;return -1;}
    memcpy(bytes+count,data,size);count+=(int)size;return (ssize_t)size;
}
int jm_test_count(void) {return count;}
int jm_test_byte(int i) {return bytes[i];}
int jm_test_transitions(void) {return transitions;}
int jm_test_state(int i,int channel) {return states[i][channel];}
