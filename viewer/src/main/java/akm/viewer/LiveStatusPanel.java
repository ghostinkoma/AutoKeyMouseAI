package akm.viewer;

import java.awt.BorderLayout;
import java.awt.Color;
import java.awt.Font;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import javax.swing.BorderFactory;
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.JScrollPane;
import javax.swing.JSplitPane;
import javax.swing.JTable;
import javax.swing.JTextArea;
import javax.swing.table.DefaultTableModel;

/**
 * 学習の状況。学習ループ (pc/tools/mirror_only.py) が 5 秒ごとに書く pc/models/live_status.txt を読んで、
 * 今の状況 (現在地・Helper・認識した窓・液晶への送信・ESP32・PT 招待・自動学習) を表に、最近の出来事をテキストに出す。
 * 書かれた時刻が古ければ「止まっている」と出す。
 */
public final class LiveStatusPanel extends JPanel {
    private static final long STALE_MS = 20_000;

    private final SightingsPanel.MapViewerContext ctx;
    private final DefaultTableModel table = new DefaultTableModel(new Object[] {"項目", "今の状況"}, 0) {
        @Override public boolean isCellEditable(int r, int c) { return false; }
    };
    private final JLabel info = new JLabel(" ");
    private final JTextArea events = new JTextArea();
    private long fileTime = -1;
    private String lastEvents = "";

    public LiveStatusPanel(SightingsPanel.MapViewerContext ctx) {
        super(new BorderLayout());
        this.ctx = ctx;
        JTable t = new JTable(table);
        t.getColumnModel().getColumn(0).setMinWidth(110);
        t.getColumnModel().getColumn(0).setPreferredWidth(130);
        t.getColumnModel().getColumn(0).setMaxWidth(160);
        t.getColumnModel().getColumn(1).setPreferredWidth(520);
        events.setEditable(false);
        events.setFont(new Font(Font.MONOSPACED, Font.PLAIN, 12));
        JScrollPane tp = new JScrollPane(t);
        JScrollPane ep = new JScrollPane(events);
        ep.setBorder(BorderFactory.createTitledBorder("最近の出来事 (認識・PT 招待のお断り・学習)"));
        JSplitPane split = new JSplitPane(JSplitPane.HORIZONTAL_SPLIT, tp, ep);
        split.setResizeWeight(0.65);
        split.setDividerLocation(0.65);
        add(info, BorderLayout.NORTH);
        add(split, BorderLayout.CENTER);
        setBorder(BorderFactory.createTitledBorder("学習の状況"));
    }

    public void refresh() {
        Path f = WindowsPanel.modelsDir(ctx.db().path).resolve("live_status.txt");
        long t;
        try {
            t = Files.exists(f) ? Files.getLastModifiedTime(f).toMillis() : 0;
        } catch (IOException ex) {
            t = 0;
        }
        long age = System.currentTimeMillis() - t;
        if (t == 0) {
            info.setForeground(Color.GRAY);
            info.setText("学習ループが動いていません (start_learn.ps1 で起動すると、ここに 5 秒ごとの状況が出ます)");
        } else if (age > STALE_MS) {
            info.setForeground(new Color(0xC0, 0x30, 0x30));
            info.setText(String.format("学習ループが止まっているようです (最後の更新 %d 秒前)", age / 1000));
        } else {
            info.setForeground(new Color(0x20, 0x80, 0x30));
            info.setText(String.format("学習ループ 動作中 (更新 %d 秒前)", age / 1000));
        }
        if (t == 0 || t == fileTime) return;
        try {
            List<String> lines = Files.readAllLines(f, StandardCharsets.UTF_8);
            fileTime = t;
            table.setRowCount(0);
            StringBuilder ev = new StringBuilder();
            boolean inEvents = false;
            for (String l : lines) {
                if (l.equals("#events")) {
                    inEvents = true;
                } else if (inEvents) {
                    ev.append(l).append('\n');
                } else if (!l.startsWith("#") && !l.isBlank()) {
                    String[] kv = l.split("\t", 2);
                    table.addRow(new Object[] {kv[0], kv.length > 1 ? kv[1] : ""});
                }
            }
            if (!ev.toString().equals(lastEvents)) {
                lastEvents = ev.toString();
                events.setText(lastEvents);
                events.setCaretPosition(events.getDocument().getLength());  // 一番新しい行を見せる
            }
        } catch (IOException ex) {
            // 学習ループが書き換えている最中。次の秒に読み直す
        }
    }
}
