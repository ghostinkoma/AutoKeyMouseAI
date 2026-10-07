package akm.viewer;

import java.awt.BorderLayout;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import javax.swing.DefaultListModel;
import javax.swing.JLabel;
import javax.swing.JList;
import javax.swing.JPanel;
import javax.swing.JScrollPane;

/**
 * 登録ログ。認識したモンスター名がマップガイドと一致して自動で DB に登録されたもの
 * (新しい出現エリア / 目撃の確定) を新しい順に表示する。1 秒ごとに増えた分だけ足す。
 */
public final class SpawnLogPanel extends JPanel {
    private static final SimpleDateFormat TIME = new SimpleDateFormat("MM/dd HH:mm:ss");
    private final SightingsPanel.MapViewerContext ctx;
    private final DefaultListModel<String> model = new DefaultListModel<>();
    private final JLabel info = new JLabel(" ");
    private long lastId = 0;
    private int unseen = 0;
    private MapDb shownDb;

    public SpawnLogPanel(SightingsPanel.MapViewerContext ctx) {
        super(new BorderLayout());
        this.ctx = ctx;
        add(new JScrollPane(new JList<>(model)), BorderLayout.CENTER);
        add(info, BorderLayout.SOUTH);
    }

    static String format(MapDb.SpawnLog e) {
        String area = e.ax() == null ? "" : String.format(" (%d-%d, %d-%d)", e.ax(), e.ax() + 9, e.ay(), e.ay() + 9);
        String kind = "area".equals(e.kind()) ? "[出現エリア]" : "confirm".equals(e.kind()) ? "[確定]" : "[" + e.kind() + "]";
        return TIME.format(new Date((long) (e.ts() * 1000))) + "  " + kind + " " + e.monster()
                + (e.level() == null ? "" : " Lv" + e.level()) + "  @ " + e.map() + area
                + (e.ratio() == null ? "" : String.format("  一致 %.0f%%", e.ratio() * 100))
                + (e.raw() == null || e.raw().equals(e.monster()) ? "" : "  (読み " + e.raw() + ")")
                + (e.detail() == null || e.detail().isEmpty() ? "" : "  " + e.detail());
    }

    /** 増えた分を足す。戻り値: 今回増えた件数。 */
    public int refresh() {
        MapDb db = ctx.db();
        if (db != shownDb) {  // DB を開き直したら最初から
            shownDb = db;
            lastId = 0;
            model.clear();
        }
        try {
            List<MapDb.SpawnLog> fresh = db.spawnLog(lastId, 500);
            for (int i = fresh.size() - 1; i >= 0; i--) {  // 古い順に先頭へ入れる → 新しいものが一番上
                model.add(0, format(fresh.get(i)));
                lastId = Math.max(lastId, fresh.get(i).id());
            }
            while (model.size() > 2000) model.remove(model.size() - 1);
            if (!fresh.isEmpty() && !isShowing()) unseen += fresh.size();
            if (isShowing()) unseen = 0;
            info.setText(model.isEmpty() ? "まだ登録はありません (ガイドと一致したモンスターを自動で DB に登録すると、ここに出ます)"
                    : "ガイドと一致して自動で DB に登録したもの (新しい順) " + model.size() + " 件");
            return fresh.size();
        } catch (Exception ex) {
            info.setText("登録ログを読めません: " + ex.getMessage());
            return 0;
        }
    }

    public int unseen() {
        return unseen;
    }

    public String latest() {
        return model.isEmpty() ? null : model.get(0);
    }
}
