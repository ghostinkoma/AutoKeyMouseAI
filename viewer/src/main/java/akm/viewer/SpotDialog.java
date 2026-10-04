package akm.viewer;

import java.awt.BorderLayout;
import java.awt.FlowLayout;
import java.awt.GridBagConstraints;
import java.awt.GridBagLayout;
import java.awt.Insets;
import java.awt.Window;
import java.util.List;
import javax.swing.JButton;
import javax.swing.JComboBox;
import javax.swing.JComponent;
import javax.swing.JDialog;
import javax.swing.JLabel;
import javax.swing.JOptionPane;
import javax.swing.JPanel;
import javax.swing.JSpinner;
import javax.swing.JTextField;
import javax.swing.SpinnerNumberModel;

/** 狩場などの地点を登録・編集する画面。名前・種類・マップ・座標・半径・メモを 1 画面で入力する。 */
public final class SpotDialog extends JDialog {
    /** 種類 (DB の値) と表示名。 */
    static final String[][] KINDS = {
        {"hunt", "狩場"}, {"shop", "薬屋・お店"}, {"town", "街"}, {"safe", "安全地帯"}, {"other", "その他"},
    };

    public record Result(String name, String kind, String map, int x, int y, int radius, String note) {}

    private final JTextField name = new JTextField(22);
    private final JComboBox<String> kind = new JComboBox<>();
    private final JComboBox<String> map = new JComboBox<>();
    private final JSpinner x = new JSpinner(new SpinnerNumberModel(0, 0, 255, 1));
    private final JSpinner y = new JSpinner(new SpinnerNumberModel(0, 0, 255, 1));
    private final JSpinner radius = new JSpinner(new SpinnerNumberModel(8, 1, 60, 1));
    private final JTextField note = new JTextField(22);
    private Result result;

    private SpotDialog(Window owner, String title, List<String> maps, MapDb.Spot init, String defMap, int defX, int defY) {
        super(owner, title, ModalityType.APPLICATION_MODAL);
        for (String[] k : KINDS) kind.addItem(k[1]);
        map.setEditable(true);
        for (String m : maps) map.addItem(m);

        if (init != null) {
            name.setText(init.name());
            kind.setSelectedIndex(kindIndex(init.kind()));
            map.setSelectedItem(init.map());
            x.setValue(init.x());
            y.setValue(init.y());
            radius.setValue(init.radius());
            note.setText(init.note() == null ? "" : init.note());
        } else {
            map.setSelectedItem(defMap);
            x.setValue(clamp(defX));
            y.setValue(clamp(defY));
            name.setText(defMap == null ? "" : defMap + " 狩場");
        }

        JPanel form = new JPanel(new GridBagLayout());
        GridBagConstraints c = new GridBagConstraints();
        c.insets = new Insets(4, 6, 4, 6);
        c.anchor = GridBagConstraints.WEST;
        int row = 0;
        row = addRow(form, c, row, "名前", name);
        row = addRow(form, c, row, "種類", kind);
        row = addRow(form, c, row, "マップ", map);
        JPanel xy = new JPanel(new FlowLayout(FlowLayout.LEFT, 4, 0));
        xy.add(new JLabel("X"));
        xy.add(x);
        xy.add(new JLabel("Y"));
        xy.add(y);
        row = addRow(form, c, row, "座標", xy);
        JPanel rp = new JPanel(new FlowLayout(FlowLayout.LEFT, 4, 0));
        rp.add(radius);
        rp.add(new JLabel("マス (狩場ならこの範囲で狩る)"));
        row = addRow(form, c, row, "半径", rp);
        addRow(form, c, row, "メモ", note);

        JButton ok = new JButton(init == null ? "登録" : "保存");
        JButton cancel = new JButton("キャンセル");
        ok.addActionListener(e -> accept());
        cancel.addActionListener(e -> dispose());
        JPanel buttons = new JPanel(new FlowLayout(FlowLayout.RIGHT));
        buttons.add(ok);
        buttons.add(cancel);

        setLayout(new BorderLayout());
        add(form, BorderLayout.CENTER);
        add(buttons, BorderLayout.SOUTH);
        getRootPane().setDefaultButton(ok);
        pack();
        setLocationRelativeTo(owner);
    }

    private static int addRow(JPanel p, GridBagConstraints c, int row, String label, JComponent comp) {
        c.gridy = row;
        c.gridx = 0;
        c.fill = GridBagConstraints.NONE;
        p.add(new JLabel(label), c);
        c.gridx = 1;
        c.fill = GridBagConstraints.HORIZONTAL;
        p.add(comp, c);
        return row + 1;
    }

    private void accept() {
        Object m = map.getEditor().getItem();
        String mapName = m == null ? "" : m.toString().trim();
        if (name.getText().isBlank() || mapName.isEmpty()) {
            JOptionPane.showMessageDialog(this, "名前とマップを入力してください");
            return;
        }
        result = new Result(name.getText().trim(), KINDS[kind.getSelectedIndex()][0], mapName,
                (Integer) x.getValue(), (Integer) y.getValue(), (Integer) radius.getValue(), note.getText().trim());
        dispose();
    }

    static int kindIndex(String k) {
        for (int i = 0; i < KINDS.length; i++) if (KINDS[i][0].equals(k)) return i;
        return KINDS.length - 1;
    }

    static String kindLabel(String k) {
        return KINDS[kindIndex(k)][1];
    }

    private static int clamp(int v) {
        return Math.max(0, Math.min(255, v));
    }

    /** 新規登録。キャンセルなら null。 */
    public static Result create(Window owner, List<String> maps, String map, int x, int y) {
        SpotDialog d = new SpotDialog(owner, "地点の登録", maps, null, map, x, y);
        d.setVisible(true);
        return d.result;
    }

    /** 編集。キャンセルなら null。 */
    public static Result edit(Window owner, List<String> maps, MapDb.Spot s) {
        SpotDialog d = new SpotDialog(owner, "地点の編集 #" + s.id(), maps, s, null, 0, 0);
        d.setVisible(true);
        return d.result;
    }
}
