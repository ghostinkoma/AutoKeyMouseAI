package akm.viewer;

import java.awt.BorderLayout;
import java.awt.FlowLayout;
import java.awt.GridBagConstraints;
import java.awt.GridBagLayout;
import java.awt.Insets;
import java.awt.Window;
import java.awt.Color;
import java.awt.Component;
import java.awt.Font;
import java.awt.Graphics2D;
import java.awt.Image;
import java.awt.RenderingHints;
import java.awt.image.BufferedImage;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.Map;
import javax.swing.DefaultListCellRenderer;
import javax.swing.ImageIcon;
import javax.swing.SwingConstants;
import java.util.List;
import java.util.function.Function;
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

    public record Result(String name, String kind, String map, int x, int y, int radius, String note, List<String> monsters) {}

    private final JTextField name = new JTextField(22);
    private final JComboBox<String> kind = new JComboBox<>();
    private final JComboBox<String> map = new JComboBox<>();
    private final JSpinner x = new JSpinner(new SpinnerNumberModel(0, 0, 255, 1));
    private final JSpinner y = new JSpinner(new SpinnerNumberModel(0, 0, 255, 1));
    private final JSpinner radius = new JSpinner(new SpinnerNumberModel(8, 1, 60, 1));
    private final JTextField note = new JTextField(22);
    private final JComboBox<Object> monsterBox = new JComboBox<>();
    private final JLabel preview = new JLabel();
    private static final String NONE = "(なし)";
    private static final Map<String, ImageIcon> ICONS = new HashMap<>();
    private String initialMonster;
    private final Function<String, List<MapDb.Monster>> monsterSource;
    /** モンスター登録画面を開く (引数: 今のマップ)。登録した名前を返す。 */
    private final java.util.function.BiFunction<Window, String, String> monsterRegistrar;
    private Result result;

    private SpotDialog(Window owner, String title, List<String> maps, Function<String, List<MapDb.Monster>> monsterSource,
                       java.util.function.BiFunction<Window, String, String> monsterRegistrar,
                       MapDb.Spot init, List<String> initMonsters, String defMap, int defX, int defY) {
        super(owner, title, ModalityType.APPLICATION_MODAL);
        this.monsterSource = monsterSource;
        this.monsterRegistrar = monsterRegistrar;
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
            initialMonster = initMonsters.isEmpty() ? null : initMonsters.get(0);
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
        row = addRow(form, c, row, "メモ", note);

        // 狙うモンスター: ドロップダウンから選ぶとアイコンが出る。登録ボタンでそのまま保存
        monsterBox.setRenderer(new DefaultListCellRenderer() {
            @Override
            public Component getListCellRendererComponent(javax.swing.JList<?> list, Object value, int index,
                                                          boolean selected, boolean focus) {
                JLabel l = (JLabel) super.getListCellRendererComponent(list, value, index, selected, focus);
                if (value instanceof MapDb.Monster m) {
                    l.setText(m.label(showMap));
                    l.setIcon(icon(m, 24));
                } else {
                    l.setIcon(null);
                }
                return l;
            }
        });
        monsterBox.setPreferredSize(new java.awt.Dimension(300, 30));
        monsterBox.setMaximumRowCount(16);
        monsterBox.addActionListener(e -> updatePreview());
        reloadMonsters();
        map.addActionListener(e -> reloadMonsters());
        JButton newMon = new JButton("モンスター登録…");
        newMon.setToolTipText("一覧に無いモンスターをアイコン・ステータス付きで登録する");
        newMon.addActionListener(e -> registerMonster());
        JPanel monRow = new JPanel(new FlowLayout(FlowLayout.LEFT, 4, 0));
        monRow.add(monsterBox);
        monRow.add(newMon);
        row = addRow(form, c, row, "モンスター", monRow);
        preview.setPreferredSize(new java.awt.Dimension(300, 110));
        preview.setHorizontalAlignment(SwingConstants.LEFT);
        preview.setFont(preview.getFont().deriveFont(Font.BOLD, 14f));
        addRow(form, c, row, "", preview);
        updatePreview();

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

    private boolean showMap;

    /** 選ばれているマップのモンスターをドロップダウンに入れる。そのマップの登録が無ければ全モンスター。 */
    private void reloadMonsters() {
        Object m = map.getEditor().getItem();
        String mapName = m == null ? "" : m.toString().trim();
        Object keep = monsterBox.getSelectedItem();
        String keepName = keep instanceof MapDb.Monster km ? km.name() : initialMonster;
        List<MapDb.Monster> list = monsterSource.apply(mapName);
        showMap = list.isEmpty();
        if (showMap) list = monsterSource.apply(null);
        monsterBox.removeAllItems();
        monsterBox.addItem(NONE);
        Object select = NONE;
        for (MapDb.Monster mon : list) {
            monsterBox.addItem(mon);
            if (mon.name().equals(keepName)) select = mon;
        }
        if (select == NONE && keepName != null) {  // 一覧に無い名前 (以前に手入力したものなど)
            MapDb.Monster extra = new MapDb.Monster(keepName, null, mapName, null, null);
            monsterBox.addItem(extra);
            select = extra;
        }
        monsterBox.setSelectedItem(select);
        monsterBox.setToolTipText(list.isEmpty() ? "モンスター一覧がありません (python tools\\import_monsters.py で取り込み)"
                : showMap ? "このマップのモンスターが無いので全マップを表示しています" : mapName + " に出るモンスター " + list.size() + " 種");
        updatePreview();
    }

    /** 一覧に無いモンスターを登録し、終わったらドロップダウンでそれを選ぶ。 */
    private void registerMonster() {
        if (monsterRegistrar == null) return;
        Object m = map.getEditor().getItem();
        String created = monsterRegistrar.apply(this, m == null ? "" : m.toString().trim());
        if (created == null) return;
        initialMonster = created;
        monsterBox.setSelectedItem(null);
        reloadMonsters();
        for (int i = 0; i < monsterBox.getItemCount(); i++)
            if (monsterBox.getItemAt(i) instanceof MapDb.Monster mm && mm.name().equals(created)) monsterBox.setSelectedIndex(i);
    }

    private void updatePreview() {
        Object v = monsterBox.getSelectedItem();
        if (v instanceof MapDb.Monster m) {
            preview.setIcon(icon(m, 96));
            preview.setText("<html>" + m.name() + (m.note() == null || m.note().isEmpty() ? "" : " <small>※" + m.note() + "</small>")
                    + "<br>Lv " + (m.level() == null ? "?" : m.level())
                    + (m.map() == null || m.map().isEmpty() ? "" : "<br>" + m.map()) + "</html>");
        } else {
            preview.setIcon(null);
            preview.setText(monsterBox.getItemCount() <= 1 ? "モンスター一覧が未登録です" : "");
        }
    }

    /** モンスターのアイコン。画像が無ければ頭文字の丸を描く。 */
    static ImageIcon icon(MapDb.Monster m, int size) {
        String key = m.name() + "|" + m.icon() + "|" + size;
        return ICONS.computeIfAbsent(key, k -> {
            if (m.icon() != null) {
                ImageIcon raw = new ImageIcon(m.icon());
                if (raw.getIconWidth() > 0) {
                    double sc = Math.min(size / (double) raw.getIconWidth(), size / (double) raw.getIconHeight());
                    if (size >= 64) sc = Math.min(sc, 3.0);  // 小さな画像は引き伸ばしすぎない
                    int w = Math.max(1, (int) (raw.getIconWidth() * sc)), h = Math.max(1, (int) (raw.getIconHeight() * sc));
                    return new ImageIcon(raw.getImage().getScaledInstance(w, h, Image.SCALE_SMOOTH));
                }
            }
            BufferedImage img = new BufferedImage(size, size, BufferedImage.TYPE_INT_ARGB);
            Graphics2D g = img.createGraphics();
            g.setRenderingHint(RenderingHints.KEY_ANTIALIASING, RenderingHints.VALUE_ANTIALIAS_ON);
            g.setRenderingHint(RenderingHints.KEY_TEXT_ANTIALIASING, RenderingHints.VALUE_TEXT_ANTIALIAS_ON);
            float hue = (m.name().hashCode() & 0xffff) / 65535f;
            g.setColor(Color.getHSBColor(hue, 0.55f, 0.75f));
            g.fillOval(1, 1, size - 2, size - 2);
            g.setColor(Color.WHITE);
            g.setFont(new Font(Font.DIALOG, Font.BOLD, (int) (size * 0.5)));
            String t = m.name().isEmpty() ? "?" : m.name().substring(0, 1);
            var fm = g.getFontMetrics();
            g.drawString(t, (size - fm.stringWidth(t)) / 2, (size - fm.getHeight()) / 2 + fm.getAscent());
            g.dispose();
            return new ImageIcon(img);
        });
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
        List<String> mons = new ArrayList<>();
        if (monsterBox.getSelectedItem() instanceof MapDb.Monster sel) mons.add(sel.name());
        result = new Result(name.getText().trim(), KINDS[kind.getSelectedIndex()][0], mapName,
                (Integer) x.getValue(), (Integer) y.getValue(), (Integer) radius.getValue(), note.getText().trim(), mons);
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
    public static Result create(Window owner, List<String> maps, Function<String, List<MapDb.Monster>> monsters,
                                java.util.function.BiFunction<Window, String, String> registrar, String map, int x, int y) {
        SpotDialog d = new SpotDialog(owner, "地点の登録", maps, monsters, registrar, null, List.of(), map, x, y);
        d.setVisible(true);
        return d.result;
    }

    /** 編集。キャンセルなら null。 */
    public static Result edit(Window owner, List<String> maps, Function<String, List<MapDb.Monster>> monsters,
                              java.util.function.BiFunction<Window, String, String> registrar, MapDb.Spot s, List<String> current) {
        SpotDialog d = new SpotDialog(owner, "地点の編集 #" + s.id(), maps, monsters, registrar, s, current, null, 0, 0);
        d.setVisible(true);
        return d.result;
    }
}
