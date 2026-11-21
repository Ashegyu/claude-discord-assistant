package kr.ashegyu.paysms;

import android.app.Activity;
import android.content.ComponentName;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.InputType;
import android.util.TypedValue;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.CompoundButton;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

/** 설정 화면 하나: 권한 상태, 웹후크 주소, 켜기/끄기, 테스트, 최근 기록. */
public class MainActivity extends Activity {
    private TextView status, hookState, logView;
    private EditText hookInput;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            refresh();
            ui.postDelayed(this, 2000);
        }
    };

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        int pad = dp(16);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(pad, pad, pad, pad);

        root.addView(title("결제 문자 전달"));
        root.addView(note("메시지 앱의 알림 중 카드 결제 문자(카드사 이름 + 승인/취소 + 금액)만 디스코드 웹후크로 보내요. "
                + "다른 문자는 폰 밖으로 나가지 않아요."));

        status = body("");
        root.addView(status);
        root.addView(button("① 알림 접근 권한 설정 열기", v -> open(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))));
        root.addView(note("\"제한된 설정\"이라며 막히면: 홈 화면에서 이 앱 아이콘을 길게 눌러 앱 정보 → 오른쪽 위 ⋮ → 제한된 설정 허용."));
        root.addView(button("② 배터리 사용 제한 풀기 (앱 정보 열기)", v -> open(
                new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + getPackageName())))));
        root.addView(note("앱 정보 → 배터리 → \"제한 없음\"으로 바꿔야 며칠 뒤에도 계속 동작해요."));

        root.addView(title2("③ 디스코드 웹후크 주소"));
        hookState = body("");
        root.addView(hookState);
        hookInput = new EditText(this);
        hookInput.setHint("https://discord.com/api/webhooks/…");
        hookInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        hookInput.setSingleLine(true);
        root.addView(hookInput);
        root.addView(button("주소 저장", v -> saveHook()));

        Switch sw = new Switch(this);
        sw.setText("전달 켜기");
        sw.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
        sw.setChecked(Store.enabled(this));
        sw.setPadding(0, dp(12), 0, dp(12));
        sw.setOnCheckedChangeListener((CompoundButton c, boolean on) -> {
            Store.setEnabled(this, on);
            Store.log(this, on ? "전달 켬" : "전달 끔");
            refresh();
        });
        root.addView(sw);

        root.addView(button("④ 테스트 보내기", v -> {
            Sender.send(this, "🔧 결제 문자 전달 앱 테스트 (이 글에는 비서가 👀 표시만 남겨요)");
            toast("보냈어요. 아래 기록과 디스코드 채널을 확인하세요.");
        }));
        root.addView(button("결제 문자 예시로 테스트 (1,000원 기록됨)", v -> {
            Sender.send(this, "신한카드(0000)승인 홍*동 1,000원(일시불) 테스트가게");
            toast("보냈어요. 비서가 기록 카드를 붙이면 [기록 지우기]를 눌러 지우세요.");
        }));

        root.addView(title2("최근 기록"));
        logView = body("");
        logView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        logView.setTextIsSelectable(true);
        root.addView(logView);

        ScrollView scroll = new ScrollView(this);
        scroll.addView(root, new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        setContentView(scroll);
    }

    @Override
    protected void onResume() {
        super.onResume();
        Sender.flush(this);
        ui.post(tick);
    }

    @Override
    protected void onPause() {
        super.onPause();
        ui.removeCallbacks(tick);
    }

    private void saveHook() {
        String url = hookInput.getText().toString().trim();
        if (!Store.validWebhook(url)) {
            toast("디스코드 웹후크 주소가 아니에요 (https://discord.com/api/webhooks/… 로 시작해야 해요)");
            return;
        }
        Store.setWebhook(this, url);
        hookInput.setText("");
        Store.log(this, "웹후크 주소 저장");
        toast("저장했어요");
        refresh();
    }

    private void refresh() {
        boolean access = hasAccess();
        int waiting = Store.queueSize(this);
        status.setText((access ? "✅ 알림 접근 권한: 허용됨" : "⚠️ 알림 접근 권한: 아직 없음 (아래 ①)")
                + "\n" + (Store.enabled(this) ? "✅ 전달: 켜짐" : "⏸ 전달: 꺼짐")
                + (waiting > 0 ? "\n⏳ 보내지 못해 기다리는 글: " + waiting + "건" : ""));
        String url = Store.webhook(this);
        hookState.setText(Store.validWebhook(url)
                ? "✅ 저장됨 (끝 4글자 …" + url.substring(Math.max(0, url.length() - 4)) + "). 바꾸려면 새 주소를 넣고 저장하세요."
                : "⚠️ 아직 없음. 디스코드 채널 편집 → 연동 → 웹후크에서 복사해 붙여 넣으세요.");
        logView.setText(Store.logText(this));
    }

    private boolean hasAccess() {
        String flat = Settings.Secure.getString(getContentResolver(), "enabled_notification_listeners");
        if (flat == null) return false;
        String me = new ComponentName(this, PayListener.class).flattenToString();
        String meShort = new ComponentName(this, PayListener.class).flattenToShortString();
        for (String s : flat.split(":")) {
            if (s.equals(me) || s.equals(meShort)) return true;
        }
        return false;
    }

    private void open(Intent i) {
        try {
            startActivity(i);
        } catch (Exception e) {
            toast("설정 화면을 열지 못했어요. 폰 설정에서 직접 찾아 주세요.");
        }
    }

    // ── 화면 조각 ──
    private int dp(int v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics());
    }

    private TextView text(String s, int sp, boolean bold, int top) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        if (bold) t.setTypeface(t.getTypeface(), android.graphics.Typeface.BOLD);
        t.setPadding(0, dp(top), 0, dp(4));
        return t;
    }

    private TextView title(String s) { return text(s, 22, true, 0); }

    private TextView title2(String s) { return text(s, 16, true, 16); }

    private TextView body(String s) { return text(s, 14, false, 6); }

    private TextView note(String s) {
        TextView t = text(s, 12, false, 2);
        t.setAlpha(0.7f);
        return t;
    }

    private Button button(String label, View.OnClickListener l) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        b.setOnClickListener(l);
        return b;
    }

    private void toast(String s) {
        Toast.makeText(this, s, Toast.LENGTH_LONG).show();
    }
}
