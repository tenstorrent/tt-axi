// file = 0; split type = patterns; threshold = 100000; total count = 0.
#include <stdio.h>
#include <stdlib.h>
#include <strings.h>
#include "rmapats.h"

void  schedNewEvent (struct dummyq_struct * I1468, EBLK  * I1463, U  I623);
void  schedNewEvent (struct dummyq_struct * I1468, EBLK  * I1463, U  I623)
{
    U  I1759;
    U  I1760;
    U  I1761;
    struct futq * I1762;
    struct dummyq_struct * pQ = I1468;
    I1759 = ((U )vcs_clocks) + I623;
    I1761 = I1759 & ((1 << fHashTableSize) - 1);
    I1463->I669 = (EBLK  *)(-1);
    I1463->I670 = I1759;
    if (0 && rmaProfEvtProp) {
        vcs_simpSetEBlkEvtID(I1463);
    }
    if (I1759 < (U )vcs_clocks) {
        I1760 = ((U  *)&vcs_clocks)[1];
        sched_millenium(pQ, I1463, I1760 + 1, I1759);
    }
    else if ((peblkFutQ1Head != ((void *)0)) && (I623 == 1)) {
        I1463->I672 = (struct eblk *)peblkFutQ1Tail;
        peblkFutQ1Tail->I669 = I1463;
        peblkFutQ1Tail = I1463;
    }
    else if ((I1762 = pQ->I1369[I1761].I692)) {
        I1463->I672 = (struct eblk *)I1762->I690;
        I1762->I690->I669 = (RP )I1463;
        I1762->I690 = (RmaEblk  *)I1463;
    }
    else {
        sched_hsopt(pQ, I1463, I1759);
    }
}
#ifdef __cplusplus
extern "C" {
#endif
void SinitHsimPats(void);
#ifdef __cplusplus
}
#endif
