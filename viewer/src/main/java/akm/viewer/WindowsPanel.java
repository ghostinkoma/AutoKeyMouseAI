package akm.viewer;

import java.awt.BorderLayout;
import java.awt.Font;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import javax.swing.BorderFactory;
import javax.swing.DefaultListModel;
import javax.swing.JLabel;
import javax.swing.JList;
import javax.swing.JPanel;
import javax.swing.JScrollPane;
import javax.swing.JSplitPane;
import javax.swing.JTextArea;

/**
 * 窓の種類の学習 (ESP32 で動かす窓の認識)。PC の pc/models/ にあるファイルを読んで表示する。
 * <ul>
 *   <li>学習できた窓 (リスト): window_table.tsv (pc/tools/window_train.py が学習のたびに書く)</li>
 *   <li>ログ (テキスト): window_log.txt (見本の追加・学習の結果・ESP32 への送信)</li>
 * </ul>
 * ファイルが変わったときだけ読み直す。
 */
public final class WindowsPanel extends JPanel {
    private static final SimpleDateFormat TIME = new SimpleDateFormat("MM/dd HH:mm:ss");
    private static final int LOG_LINES = 400;

    private final SightingsPanel.MapViewerContext ctx;
    private final DefaultListModel<String> model = new DefaultListModel<>();
    private final JLabel listInfo = new JLabel(" ");
    private final JTextArea log = new JTextArea();
    private long tableTime = -1, logTime = -1;
    private Path shownDir;

    public WindowsPanel(SightingsPanel.MapViewerContext ctx) {
        super(new BorderLayout());
        this.ctx = ctx;
        JList<String> list = new JList<>(model);
        list.setFont(new Font(Font.MONOSPACED, Font.PLAIN, 12));
        JPanel listPane = new JPanel(new BorderLayout());
        listPane.add(new JScrollPane(list), BorderLayout.CENTER);
        listPane.add(listInfo, BorderLayout.SOUTH);
        listPane.setBorder(BorderFactory.createTitledBorder("学習できた窓 (ESP32 で判定する窓)"));

        log.setEditable(false);
        log.setLineWrap(true);
        log.setFont(new Font(Font.MONOSPACED, Font.PLAIN, 12));
        JScrollPane logPane = new JScrollPane(log);
        logPane.setBorder(BorderFactory.createTitledBorder("窓の種類の学習のログ"));

        JSplitPane split = new JSplitPane(JSplitPane.VERTICAL_SPLIT, listPane, logPane);
        split.setResizeWeight(0.4);
        split.setDividerLocation(0.4);
        add(split, BorderLayout.CENTER);
    }

    /** DB (pc/data/mu_map.db) と同じ pc の下の models/。 */
    static Path modelsDir(String dbPath) {
        Path db = Path.of(dbPath).toAbsolutePath();
        Path pc = db.getParent() == null ? null : db.getParent().getParent();
        return pc == null ? Path.of("models") : pc.resolve("models");
    }

    /** window_table.tsv の 1 行 → リストの 1 行。 */
    static String format(String[] c) {
        String name = c.length > 0 ? c[0] : "?";
        String titles = c.length > 1 && !c[1].isEmpty() ? c[1] : "-";
        String slot = c.length > 2 ? c[2] : "";
        String count = c.length > 3 ? c[3] : "0";
        String src = c.length > 4 ? c[4] : "";
        String test = c.length > 5 && !c[5].isEmpty() ? "評価 " + c[5] : "評価なし";
        return String.format("%-16s 見本 %4s (%s)  %s  場所 %s  [%s]", name, count, src, test, slot, titles);
    }

    static List<String> tableRows(Path tsv) throws IOException {
        List<String> out = new ArrayList<>();
        List<String> lines = Files.readAllLines(tsv, StandardCharsets.UTF_8);
        for (int i = 1; i < lines.size(); i++) {  // 1 行目は見出し
            String l = lines.get(i);
            if (!l.isBlank()) out.add(format(l.split("\t", -1)));
        }
        return out;
    }

    public void refresh() {
        Path dir = modelsDir(ctx.db().path);
        if (!dir.equals(shownDir)) {
            shownDir = dir;
            tableTime = logTime = -1;
        }
        Path tsv = dir.resolve("window_table.tsv");
        try {
            long t = Files.exists(tsv) ? Files.getLastModifiedTime(tsv).toMillis() : 0;
            if (t != tableTime) {
                tableTime = t;
                model.clear();
                if (t == 0) {
                    listInfo.setText("まだ学習していません (学習ループが 30 分ごとに学習すると、ここに出ます)");
                } else {
                    for (String r : tableRows(tsv)) model.addElement(r);
                    listInfo.setText("学習できた窓 " + model.size() + " 種  (更新 " + TIME.format(new Date(t)) + ")");
                }
            }
        } catch (IOException ex) {
            listInfo.setText("窓の一覧を読めません: " + ex.getMessage());
        }
        Path lf = dir.resolve("window_log.txt");
        try {
            long t = Files.exists(lf) ? Files.getLastModifiedTime(lf).toMillis() : 0;
            if (t != logTime) {
                logTime = t;
                if (t == 0) {
                    log.setText("(まだ記録はありません: " + lf + ")");
                } else {
                    List<String> lines = Files.readAllLines(lf, StandardCharsets.UTF_8);
                    int from = Math.max(0, lines.size() - LOG_LINES);
                    log.setText(String.join("\n", lines.subList(from, lines.size())));
                    log.setCaretPosition(log.getDocument().getLength());  // 一番新しい行を見せる
                }
            }
        } catch (IOException ex) {
            log.setText("ログを読めません: " + ex.getMessage());
        }
    }
}
