package akm.viewer;

import java.awt.BorderLayout;
import java.awt.FlowLayout;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import javax.swing.JButton;
import javax.swing.JCheckBox;
import javax.swing.JLabel;
import javax.swing.JOptionPane;
import javax.swing.JPanel;
import javax.swing.JScrollPane;
import javax.swing.JTable;
import javax.swing.table.DefaultTableModel;

/**
 * 新しいマップ。ゲーム画面右下に知らないマップ名が続けて読めると、Python (akm/world_log.py) が候補として残す。
 * 承認 (または手で登録) すると既知のマップになり、そのマップでの位置の記録が始まる。
 */
public final class MapsPanel extends JPanel {
    private static final SimpleDateFormat TIME = new SimpleDateFormat("MM/dd HH:mm:ss");
    private final SightingsPanel.MapViewerContext ctx;
    private final JCheckBox unchecked = new JCheckBox("未確認だけ", true);
    private final JLabel info = new JLabel(" ");
    private final DefaultTableModel model = new DefaultTableModel(new Object[] {"マップ名", "状態", "読み", "回数", "最後に読んだ", "座標"}, 0) {
        @Override public boolean isCellEditable(int r, int c) { return false; }
    };
    private final JTable table = new JTable(model);
    private List<MapDb.MapName> rows = List.of();

    public MapsPanel(SightingsPanel.MapViewerContext ctx) {
        super(new BorderLayout());
        this.ctx = ctx;
        table.addMouseListener(new java.awt.event.MouseAdapter() {
            @Override public void mouseClicked(java.awt.event.MouseEvent e) {
                if (e.getClickCount() == 2) review(true, true);
            }
        });
        JPanel buttons = new JPanel(new FlowLayout(FlowLayout.LEFT, 4, 2));
        JButton ok = new JButton("承認");
        ok.addActionListener(e -> review(true, false));
        JButton fix = new JButton("名前を直して承認…");
        fix.addActionListener(e -> review(true, true));
        JButton ng = new JButton("却下");
        ng.addActionListener(e -> review(false, false));
        JButton add = new JButton("マップを手で登録…");
        add.addActionListener(e -> addManual(this, ctx));
        unchecked.addActionListener(e -> refresh());
        buttons.add(ok);
        buttons.add(fix);
        buttons.add(ng);
        buttons.add(add);
        buttons.add(unchecked);
        JPanel south = new JPanel(new BorderLayout());
        south.add(buttons, BorderLayout.NORTH);
        south.add(info, BorderLayout.SOUTH);
        add(new JScrollPane(table), BorderLayout.CENTER);
        add(south, BorderLayout.SOUTH);
    }

    /** マップ名を入力して既知のマップに登録する (ゲーム画面右下の表示どおり英語で)。 */
    public static String addManual(java.awt.Component parent, SightingsPanel.MapViewerContext ctx) {
        String name = JOptionPane.showInputDialog(parent, "マップ名 (ゲーム画面右下の表示どおり英語で。例: Karutan)", "マップ登録",
                JOptionPane.QUESTION_MESSAGE);
        if (name == null || name.isBlank()) return null;
        try {
            ctx.db().addMap(name.trim());
            ctx.changed();
            return name.trim();
        } catch (Exception ex) {
            JOptionPane.showMessageDialog(parent, "登録できません: " + ex.getMessage());
            return null;
        }
    }

    public void refresh() {
        try {
            List<MapDb.MapName> list = ctx.db().mapNames(unchecked.isSelected());
            info.setText("知らないマップ名が続けて読めると候補になります。承認するとそのマップの位置を記録し始めます");
            if (list.equals(rows)) return;
            rows = list;
            model.setRowCount(0);
            for (MapDb.MapName m : rows)
                model.addRow(new Object[] {m.name(),
                        (m.status().equals("ok") ? "登録" : m.status().equals("ng") ? "却下" : "候補") + (m.reviewed() ? "(人)" : ""),
                        m.raw() == null ? "" : m.raw(), m.reads(),
                        m.lastSeen() == null ? "" : TIME.format(new Date((long) (m.lastSeen() * 1000))),
                        m.x() == null ? "" : m.x() + ", " + m.y()});
        } catch (Exception ex) {
            info.setText("読めません: " + ex.getMessage());
        }
    }

    private void review(boolean ok, boolean askName) {
        int[] sel = table.getSelectedRows();
        if (sel.length == 0) {
            JOptionPane.showMessageDialog(this, "一覧から選んでください");
            return;
        }
        String name = null;
        if (askName) {
            name = (String) JOptionPane.showInputDialog(this, "正しいマップ名 (ゲームの表示どおり英語で)", "名前を直して承認",
                    JOptionPane.QUESTION_MESSAGE, null, null, rows.get(sel[0]).name());
            if (name == null || name.isBlank()) return;
        }
        try {
            for (int r : sel) ctx.db().reviewMap(rows.get(r), ok, name);
        } catch (Exception ex) {
            JOptionPane.showMessageDialog(this, "保存できません: " + ex.getMessage());
        }
        rows = List.of();
        refresh();
        ctx.changed();
    }
}
