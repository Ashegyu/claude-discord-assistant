package kr.ashegyu.paysms;

import android.app.Notification;
import android.os.Bundle;
import android.os.Parcelable;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;

/** 메시지 앱의 알림을 읽어, 카드 결제 문자만 디스코드 웹후크로 전달한다. */
public class PayListener extends NotificationListenerService {
    /** 알림을 읽을 앱: 삼성 메시지, 구글 메시지. 다른 앱의 알림은 내용을 보지 않는다. */
    static final Set<String> SOURCES = new HashSet<>(Arrays.asList(
            "com.samsung.android.messaging", "com.google.android.apps.messaging"));

    @Override
    public void onNotificationPosted(StatusBarNotification sbn) {
        try {
            if (sbn == null || !SOURCES.contains(sbn.getPackageName())) return;
            if (!Store.enabled(this)) return;
            Notification n = sbn.getNotification();
            if (n == null || (n.flags & Notification.FLAG_GROUP_SUMMARY) != 0) return;
            String text = extract(n.extras);
            if (!Filter.isPayment(text)) return;
            if (Store.seenRecently(this, text)) return;
            Sender.send(this, text);
        } catch (Exception e) {
            Store.log(this, "✗ 알림 처리 오류: " + e.getClass().getSimpleName());
        }
    }

    @Override
    public void onListenerConnected() {
        Store.log(this, "알림 읽기 연결됨");
        Sender.flush(this);
    }

    /** 알림에서 문자 본문을 가장 긴 형태로 꺼낸다 (대화형 알림 → 펼친 글 → 한 줄 글 순서). */
    static String extract(Bundle ex) {
        if (ex == null) return "";
        String best = "";
        Parcelable[] msgs = ex.getParcelableArray(Notification.EXTRA_MESSAGES);
        if (msgs != null && msgs.length > 0 && msgs[msgs.length - 1] instanceof Bundle) {
            CharSequence t = ((Bundle) msgs[msgs.length - 1]).getCharSequence("text");
            if (t != null) best = t.toString();
        }
        for (String key : new String[]{Notification.EXTRA_BIG_TEXT, Notification.EXTRA_TEXT}) {
            CharSequence t = ex.getCharSequence(key);
            if (t != null && t.length() > best.length()) best = t.toString();
        }
        CharSequence[] lines = ex.getCharSequenceArray(Notification.EXTRA_TEXT_LINES);
        if (best.isEmpty() && lines != null && lines.length > 0 && lines[lines.length - 1] != null) {
            best = lines[lines.length - 1].toString();
        }
        return best.trim();
    }
}
