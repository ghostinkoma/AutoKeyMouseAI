package akm.viewer;

import java.awt.BorderLayout;
import java.awt.Color;
import java.awt.Component;
import java.awt.FlowLayout;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import javax.swing.JButton;
import javax.swing.JCheckBox;
import javax.swing.JLabel;
import javax.swing.JOptionPane;
import javax.swing.JPanel;
import javax.swing.JScrollPane;
import javax.swing.JTable;
import javax.swing.ListSelectionModel;
import javax.swing.table.DefaultTableCellRenderer;
import javax.swing.table.DefaultTableModel;

/**
 * 目撃一覧。ゲーム画面から読んだ相手 (モンスター) の名前を、判定 AI (pc/akm/record_filter.py) が
 * 登録 (ok) / 保留 (pending) / 除外 (ng) に振り分けたもの。保留を人が承認 / 却下すると、
 * 承認したものはモンスター一覧に入り、どちらも次の学習 (python tools\record_filter.py train) の正解になる。
 */
public final class SightingsPanel extends JPanel {
    private static final SimpleDateFormat TIME = new SimpleDateFormat("MM/dd HH:mm:ss");
    private final MapViewerContext ctx;
    private final JCheckBox onlyPending = new JCheckBox("未確認 (保留・除外) だけ", true);
    private final JLabel info = new JLabel(" ");
    private final DefaultTableModel model = new DefaultTableModel(
            new Object[] {"時刻", "判定", "名前", "読み", "Lv", "場所", "回数", "確率"}, 0) {
        @Override public boolean isCellEditable(int r, int c) { return false; }
    };
    private final JTable table = new JTable(model);
    private List<MapDb.Sighting> rows = List.of();

    /** 一覧が使う DB (DB を開き直しても追従できるように取り出し口で受け取る)。 */
    public interface MapViewerContext {
        MapDb db();
        void changed();
    }

    public SightingsPanel(MapViewerContext ctx) {
        super(new BorderLayout());
        this.ctx = ctx;
        table.setSelectionMode(ListSelectionModel.MULTIPLE_INTERVAL_SELECTION);
        table.setAutoCreateRowSorter(false);
        int[] widths = {95, 45, 130, 110, 35, 120, 40, 45};
        for (int i = 0; i < widths.length; i++) table.getColumnModel().getColumn(i).setPreferredWidth(widths[i]);
        table.getColumnModel().getColumn(1).setCellRenderer(new DefaultTableCellRenderer() {
            @Override public Component getTableCellRendererComponent(JTable t, Object v, boolean sel, boolean f, int r, int c) {
                Component comp = super.getTableCellRendererComponent(t, v, sel, f, r, c);
                if (!sel) {
                    String s = String.valueOf(v);
                    comp.setForeground(s.startsWith("登録") ? new Color(0, 140, 0) : s.startsWith("除外") ? Color.GRAY
                            : new Color(200, 110, 0));
                }
                return comp;
            }
        });
        table.addMouseListener(new java.awt.event.MouseAdapter() {
            @Override public void mouseClicked(java.awt.event.MouseEvent e) {
                if (e.getClickCount() == 2) review(true, true);
            }
        });
        JPanel buttons = new JPanel(new FlowLayout(FlowLayout.LEFT, 4, 2));
        JButton ok = new JButton("承認");
        ok.setToolTipText("正しい名前として登録 (モンスター一覧に入る)");
        ok.addActionListener(e -> review(true, false));
        JButton fix = new JButton("名前を直して承認…");
        fix.addActionListener(e -> review(true, true));
        JButton ng = new JButton("却下");
        ng.setToolTipText("読み違い。登録しない");
        ng.addActionListener(e -> review(false, false));
        onlyPending.addActionListener(e -> refresh());
        buttons.add(ok);
        buttons.add(fix);
        buttons.add(ng);
        buttons.add(onlyPending);
        JPanel south = new JPanel(new BorderLayout());
        south.add(buttons, BorderLayout.NORTH);
        south.add(info, BorderLayout.SOUTH);
        add(new JScrollPane(table), BorderLayout.CENTER);
        add(south, BorderLayout.SOUTH);
    }

    private static String statusLabel(MapDb.Sighting s) {
        String b = switch (s.status()) {
            case "ok" -> "登録";
            case "ng" -> "除外";
            default -> "保留";
        };
        return b + (s.reviewed() ? "(人)" : "");
    }

    public void refresh() {
        MapDb db = ctx.db();
        try {
            List<MapDb.Sighting> list = db.sightings(onlyPending.isSelected(), 300);
            int pending = db.pendingSightings();
            info.setText("保留 " + pending + " 件  ダブルクリックで名前を直して承認。判定は次の学習で AI の正解になります");
            if (list.equals(rows)) return;
            List<Long> sel = new ArrayList<>();
            for (int r : table.getSelectedRows()) if (r < rows.size()) sel.add(rows.get(r).id());
            rows = list;
            model.setRowCount(0);
            for (MapDb.Sighting s : rows) {
                String where = s.map() == null ? "?" : s.map() + (s.x() == null ? "" : " (" + s.x() + ", " + s.y() + ")");
                model.addRow(new Object[] {TIME.format(new Date((long) (s.ts() * 1000))), statusLabel(s), s.monster(),
                        s.raw() == null || s.raw().equals(s.monster()) ? "" : s.raw(),
                        s.level() == null ? "" : s.level(), where, s.reads(),
                        s.score() == null ? "" : String.format("%.2f", s.score())});
            }
            for (int i = 0; i < rows.size(); i++)
                if (sel.contains(rows.get(i).id())) table.addRowSelectionInterval(i, i);
        } catch (Exception ex) {
            info.setText("目撃を読めません: " + ex.getMessage());
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
            MapDb.Sighting first = rows.get(sel[0]);
            name = (String) JOptionPane.showInputDialog(this, "正しい名前 (ゲームの表示どおり英語で)", "名前を直して承認",
                    JOptionPane.QUESTION_MESSAGE, null, null, first.monster());
            if (name == null || name.isBlank()) return;
        }
        try {
            for (int r : sel) ctx.db().reviewSighting(rows.get(r), ok, name);
        } catch (Exception ex) {
            JOptionPane.showMessageDialog(this, "保存できません: " + ex.getMessage(), "エラー", JOptionPane.ERROR_MESSAGE);
        }
        rows = List.of();
        refresh();
        ctx.changed();
    }
}
