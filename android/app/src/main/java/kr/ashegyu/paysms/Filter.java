package kr.ashegyu.paysms;

import java.util.regex.Pattern;

/** 카드 결제 문자인지 폰 안에서 먼저 가린다. 해당하지 않는 문자는 폰 밖으로 보내지 않는다. */
final class Filter {
    private static final Pattern CARD = Pattern.compile("신한|KB|국민|삼성|현대|롯데|하나|우리|농협|NH|BC|비씨|카드|체크");
    private static final Pattern ACTION = Pattern.compile("승인|취소");
    private static final Pattern MONEY = Pattern.compile("\\d[\\d,]*\\s*원");
    private static final Pattern NOISE = Pattern.compile("\\(광고\\)|인증번호|결제\\s*예정|명세서");

    private Filter() {}

    static boolean isPayment(String text) {
        if (text == null || text.length() < 8) return false;
        if (NOISE.matcher(text).find()) return false;
        return CARD.matcher(text).find() && ACTION.matcher(text).find() && MONEY.matcher(text).find();
    }
}
