package kr.ashegyu.paysms;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/** 앱 설정·대기열·기록 (SharedPreferences). 웹후크 주소는 이 앱 안에만 저장된다. */
final class Store {
    static final String PREFS = "paysms";
    static final int LOG_MAX = 30;
    static final int QUEUE_MAX = 50;
    static final long QUEUE_TTL_MS = 3L * 24 * 3600 * 1000;   // 3일 넘은 대기 글은 버린다
    static final long SEEN_MS = 10L * 60 * 1000;              // 같은 글은 10분 안에 한 번만

    private Store() {}

    static SharedPreferences prefs(Context c) {
        return c.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static String webhook(Context c) { return prefs(c).getString("webhook", ""); }

    static void setWebhook(Context c, String url) { prefs(c).edit().putString("webhook", url).apply(); }

    static boolean enabled(Context c) { return prefs(c).getBoolean("enabled", true); }

    static void setEnabled(Context c, boolean on) { prefs(c).edit().putBoolean("enabled", on).apply(); }

    static boolean validWebhook(String url) {
        return url != null && (url.startsWith("https://discord.com/api/webhooks/")
                || url.startsWith("https://discordapp.com/api/webhooks/")
                || url.startsWith("https://canary.discord.com/api/webhooks/")
                || url.startsWith("https://ptb.discord.com/api/webhooks/"));
    }

    // ── 기록 (화면 표시용, 본문은 앞 몇 글자만) ──
    static synchronized void log(Context c, String line) {
        try {
            JSONArray old = new JSONArray(prefs(c).getString("log", "[]"));
            JSONArray out = new JSONArray();
            String ts = new SimpleDateFormat("MM/dd HH:mm:ss", Locale.KOREA).format(new Date());
            out.put(ts + "  " + line);
            for (int i = 0; i < old.length() && out.length() < LOG_MAX; i++) out.put(old.getString(i));
            prefs(c).edit().putString("log", out.toString()).apply();
        } catch (Exception ignored) { }
    }

    static synchronized String logText(Context c) {
        try {
            JSONArray a = new JSONArray(prefs(c).getString("log", "[]"));
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < a.length(); i++) sb.append(a.getString(i)).append('\n');
            return sb.length() == 0 ? "(아직 기록 없음)" : sb.toString();
        } catch (Exception e) {
            return "(기록을 읽지 못했어요)";
        }
    }

    // ── 같은 글 중복 방지 ──
    static synchronized boolean seenRecently(Context c, String text) {
        long now = System.currentTimeMillis();
        String key = Integer.toHexString(text.replaceAll("\\s+", "").hashCode());
        try {
            JSONObject old = new JSONObject(prefs(c).getString("seen", "{}"));
            JSONObject keep = new JSONObject();
            boolean hit = false;
            java.util.Iterator<String> it = old.keys();
            while (it.hasNext()) {
                String k = it.next();
                long t = old.optLong(k, 0);
                if (now - t <= SEEN_MS) {
                    keep.put(k, t);
                    if (k.equals(key)) hit = true;
                }
            }
            if (!hit) keep.put(key, now);
            prefs(c).edit().putString("seen", keep.toString()).apply();
            return hit;
        } catch (Exception e) {
            return false;
        }
    }

    // ── 전송 대기열 (통신이 끊겼을 때 보관했다가 다시 보냄) ──
    static synchronized void enqueue(Context c, String text) {
        try {
            JSONArray q = new JSONArray(prefs(c).getString("queue", "[]"));
            JSONObject o = new JSONObject();
            o.put("t", System.currentTimeMillis());
            o.put("text", text);
            q.put(o);
            while (q.length() > QUEUE_MAX) q.remove(0);
            prefs(c).edit().putString("queue", q.toString()).apply();
        } catch (Exception ignored) { }
    }

    /** 대기열을 통째로 꺼내고 비운다 (오래된 것은 버림). */
    static synchronized JSONArray drain(Context c) {
        JSONArray out = new JSONArray();
        try {
            JSONArray q = new JSONArray(prefs(c).getString("queue", "[]"));
            long now = System.currentTimeMillis();
            for (int i = 0; i < q.length(); i++) {
                JSONObject o = q.getJSONObject(i);
                if (now - o.optLong("t", 0) <= QUEUE_TTL_MS) out.put(o);
            }
            prefs(c).edit().putString("queue", "[]").apply();
        } catch (Exception ignored) { }
        return out;
    }

    static synchronized int queueSize(Context c) {
        try {
            return new JSONArray(prefs(c).getString("queue", "[]")).length();
        } catch (Exception e) {
            return 0;
        }
    }
}
