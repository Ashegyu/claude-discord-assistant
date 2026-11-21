package kr.ashegyu.paysms;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** 디스코드 웹후크로 글을 보낸다. 실패하면 대기열에 넣고 다음 기회에 다시 보낸다. */
final class Sender {
    private static final ExecutorService IO = Executors.newSingleThreadExecutor();

    private Sender() {}

    /** 새 글을 보내고, 그 전에 밀린 글이 있으면 먼저 보낸다. */
    static void send(Context ctx, String text) {
        final Context c = ctx.getApplicationContext();
        IO.execute(() -> {
            flushLocked(c);
            deliver(c, text, true);
        });
    }

    /** 밀린 글만 다시 보낸다 (앱을 열 때 등). */
    static void flush(Context ctx) {
        final Context c = ctx.getApplicationContext();
        IO.execute(() -> flushLocked(c));
    }

    private static void flushLocked(Context c) {
        JSONArray q = Store.drain(c);
        for (int i = 0; i < q.length(); i++) {
            JSONObject o = q.optJSONObject(i);
            if (o != null) deliver(c, o.optString("text", ""), true);
        }
    }

    private static void deliver(Context c, String text, boolean queueOnFail) {
        if (text == null || text.isEmpty()) return;
        String url = Store.webhook(c);
        String head = preview(text);
        if (!Store.validWebhook(url)) {
            Store.log(c, "✗ 웹후크 주소 없음 · " + head);
            if (queueOnFail) Store.enqueue(c, text);
            return;
        }
        int code = -1;
        String err = "";
        for (int attempt = 0; attempt < 3; attempt++) {
            try {
                code = post(url, text);
                if (code >= 200 && code < 300) {
                    Store.log(c, "✓ 전달 · " + head);
                    return;
                }
                if (code == 401 || code == 403 || code == 404) break;   // 주소가 틀렸거나 웹후크가 지워짐: 다시 해도 소용없다
            } catch (Exception e) {
                err = e.getClass().getSimpleName();
            }
            try {
                Thread.sleep(2000L * (attempt + 1));
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                break;
            }
        }
        boolean dead = code == 401 || code == 403 || code == 404;
        Store.log(c, "✗ 실패(" + (code > 0 ? String.valueOf(code) : err) + ")" + (dead ? " 웹후크 주소 확인" : " 나중에 다시") + " · " + head);
        if (queueOnFail && !dead) Store.enqueue(c, text);
    }

    private static int post(String url, String text) throws Exception {
        JSONObject body = new JSONObject();
        body.put("content", text.length() > 1900 ? text.substring(0, 1900) : text);
        body.put("allowed_mentions", new JSONObject().put("parse", new JSONArray()));   // @everyone 같은 멘션이 걸리지 않게
        byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
        HttpURLConnection con = (HttpURLConnection) new URL(url).openConnection();
        try {
            con.setRequestMethod("POST");
            con.setConnectTimeout(10000);
            con.setReadTimeout(10000);
            con.setDoOutput(true);
            con.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            con.setFixedLengthStreamingMode(bytes.length);
            try (OutputStream os = con.getOutputStream()) {
                os.write(bytes);
            }
            return con.getResponseCode();
        } finally {
            con.disconnect();
        }
    }

    /** 기록에는 본문 앞부분만 남긴다. */
    private static String preview(String text) {
        String one = text.replaceAll("\\s+", " ").trim();
        return one.length() > 18 ? one.substring(0, 18) + "…" : one;
    }
}
