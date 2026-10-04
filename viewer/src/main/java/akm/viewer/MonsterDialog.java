package akm.viewer;

import java.awt.BorderLayout;
import java.awt.Color;
import java.awt.Dimension;
import java.awt.FlowLayout;
import java.awt.GridBagConstraints;
import java.awt.GridBagLayout;
import java.awt.Image;
import java.awt.Insets;
import java.awt.Toolkit;
import java.awt.Window;
import java.awt.datatransfer.DataFlavor;
import java.awt.image.BufferedImage;
import java.io.File;
import java.util.ArrayList;
import java.util.List;
import javax.imageio.ImageIO;
import javax.swing.BorderFactory;
import javax.swing.DefaultListModel;
import javax.swing.ImageIcon;
import javax.swing.JButton;
import javax.swing.JComboBox;
import javax.swing.JComponent;
import javax.swing.JDialog;
import javax.swing.JFileChooser;
import javax.swing.JLabel;
import javax.swing.JList;
import javax.swing.JOptionPane;
import javax.swing.JPanel;
import javax.swing.JScrollPane;
import javax.swing.JTextField;
import javax.swing.SwingConstants;
import javax.swing.filechooser.FileNameExtensionFilter;

/**
 * モンスター登録画面。一覧に無いモンスターのアイコン・名前・レベル・ステータス・出現マップを登録する。
 * 「登録」で保存して閉じる (狩場登録画面のドロップダウンで、登録したモンスターが選ばれた状態になる)。
 */
public final class MonsterDialog extends JDialog {
    private final MapDb db;
    private final JTextField name = new JTextField(18);
    private final JTextField level = new JTextField(5);
    private final JTextField hp = new JTextField(8);
    private final JTextField atkMin = new JTextField(6);
    private final JTextField atkMax = new JTextField(6);
    private final JTextField def = new JTextField(6);
    private final JTextField defRate = new JTextField(6);
    private final JTextField atkRate = new JTextField(6);
    private final JTextField note = new JTextField(18);
    private final JComboBox<String> mapBox = new JComboBox<>();
    private final DefaultListModel<String> maps = new DefaultListModel<>();
    private final JLabel iconView = new JLabel("アイコン未設定", SwingConstants.CENTER);
    private BufferedImage icon;
    private String result;

    private MonsterDialog(Window owner, MapDb db, String map) {
        super(owner, "モンスター登録", ModalityType.APPLICATION_MODAL);
        this.db = db;

        // アイコン: ファイルから / クリップボードの画像を貼り付け (Win+Shift+S で切り取ってそのまま貼れる)
        iconView.setPreferredSize(new Dimension(110, 110));
        iconView.setBorder(BorderFactory.createLineBorder(Color.GRAY));
        JButton fromFile = new JButton("画像ファイル…");
        fromFile.addActionListener(e -> chooseIcon());
        JButton paste = new JButton("貼り付け");
        paste.setToolTipText("クリップボードの画像を使う (ゲーム画面を Win+Shift+S で切り取ってから押す)");
        paste.addActionListener(e -> pasteIcon());
        JPanel iconButtons = new JPanel(new FlowLayout(FlowLayout.CENTER, 4, 2));
        iconButtons.add(fromFile);
        iconButtons.add(paste);
        JPanel iconPane = new JPanel(new BorderLayout(0, 4));
        iconPane.add(iconView, BorderLayout.CENTER);
        iconPane.add(iconButtons, BorderLayout.SOUTH);
        iconPane.setBorder(BorderFactory.createTitledBorder("アイコン"));

        JPanel form = new JPanel(new GridBagLayout());
        GridBagConstraints c = new GridBagConstraints();
        c.insets = new Insets(3, 6, 3, 6);
        c.anchor = GridBagConstraints.WEST;
        int row = 0;
        row = addRow(form, c, row, "名前 *", name);
        row = addRow(form, c, row, "レベル", level);
        row = addRow(form, c, row, "生命", hp);
        row = addRow(form, c, row, "攻撃力", pair(atkMin, "～", atkMax));
        row = addRow(form, c, row, "防御力", def);
        row = addRow(form, c, row, "防御成功率", defRate);
        row = addRow(form, c, row, "攻撃成功率", atkRate);
        row = addRow(form, c, row, "備考", note);

        // 出現マップ (複数可)
        mapBox.setEditable(true);
        try {
            for (String m : db.monsterMaps()) mapBox.addItem(m);
        } catch (Exception ignored) {
        }
        mapBox.setSelectedItem(map == null ? "" : map);
        if (map != null && !map.isBlank()) maps.addElement(map);
        JButton addMap = new JButton("追加");
        addMap.addActionListener(e -> {
            Object v = mapBox.getEditor().getItem();
            String m = v == null ? "" : v.toString().trim();
            if (!m.isEmpty() && !maps.contains(m)) maps.addElement(m);
        });
        JList<String> mapList = new JList<>(maps);
        mapList.setVisibleRowCount(3);
        JButton delMap = new JButton("外す");
        delMap.addActionListener(e -> {
            for (String v : mapList.getSelectedValuesList()) maps.removeElement(v);
        });
        JPanel mapPane = new JPanel(new BorderLayout(4, 2));
        JPanel mapTop = new JPanel(new FlowLayout(FlowLayout.LEFT, 4, 0));
        mapTop.add(mapBox);
        mapTop.add(addMap);
        mapPane.add(mapTop, BorderLayout.NORTH);
        mapPane.add(new JScrollPane(mapList), BorderLayout.CENTER);
        mapPane.add(delMap, BorderLayout.EAST);
        addRow(form, c, row, "出現マップ", mapPane);

        JButton ok = new JButton("登録");
        JButton cancel = new JButton("キャンセル");
        ok.addActionListener(e -> save());
        cancel.addActionListener(e -> dispose());
        JPanel buttons = new JPanel(new FlowLayout(FlowLayout.RIGHT));
        buttons.add(ok);
        buttons.add(cancel);

        JPanel body = new JPanel(new BorderLayout(8, 0));
        body.setBorder(BorderFactory.createEmptyBorder(6, 6, 0, 6));
        body.add(iconPane, BorderLayout.WEST);
        body.add(form, BorderLayout.CENTER);
        setLayout(new BorderLayout());
        add(body, BorderLayout.CENTER);
        add(buttons, BorderLayout.SOUTH);
        getRootPane().setDefaultButton(ok);
        pack();
        setLocationRelativeTo(owner);
    }

    private static JPanel pair(JComponent a, String sep, JComponent b) {
        JPanel p = new JPanel(new FlowLayout(FlowLayout.LEFT, 4, 0));
        p.add(a);
        p.add(new JLabel(sep));
        p.add(b);
        return p;
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

    private void setIcon(BufferedImage img) {
        icon = img;
        double sc = Math.min(100.0 / img.getWidth(), 100.0 / img.getHeight());
        int w = Math.max(1, (int) (img.getWidth() * sc)), h = Math.max(1, (int) (img.getHeight() * sc));
        iconView.setText("");
        iconView.setIcon(new ImageIcon(img.getScaledInstance(w, h, Image.SCALE_SMOOTH)));
    }

    private void chooseIcon() {
        JFileChooser fc = new JFileChooser();
        fc.setFileFilter(new FileNameExtensionFilter("画像 (png, jpg, gif, bmp)", "png", "jpg", "jpeg", "gif", "bmp"));
        if (fc.showOpenDialog(this) != JFileChooser.APPROVE_OPTION) return;
        try {
            BufferedImage img = ImageIO.read(fc.getSelectedFile());
            if (img == null) throw new IllegalArgumentException("画像として読めません");
            setIcon(img);
        } catch (Exception ex) {
            JOptionPane.showMessageDialog(this, "読み込めません: " + ex.getMessage());
        }
    }

    private void pasteIcon() {
        try {
            var cb = Toolkit.getDefaultToolkit().getSystemClipboard();
            if (!cb.isDataFlavorAvailable(DataFlavor.imageFlavor)) {
                JOptionPane.showMessageDialog(this, "クリップボードに画像がありません\n(ゲーム画面を Win+Shift+S で切り取ってから押してください)");
                return;
            }
            Image img = (Image) cb.getData(DataFlavor.imageFlavor);
            BufferedImage b = new BufferedImage(img.getWidth(null), img.getHeight(null), BufferedImage.TYPE_INT_ARGB);
            b.getGraphics().drawImage(img, 0, 0, null);
            setIcon(b);
        } catch (Exception ex) {
            JOptionPane.showMessageDialog(this, "貼り付けできません: " + ex.getMessage());
        }
    }

    private Integer num(JTextField f, String label) {
        String t = f.getText().trim().replace(",", "");
        if (t.isEmpty()) return null;
        try {
            return Integer.parseInt(t);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(label + " は数字で入力してください");
        }
    }

    private void save() {
        String n = name.getText().trim();
        if (n.isEmpty()) {
            JOptionPane.showMessageDialog(this, "名前を入力してください");
            return;
        }
        Object pending = mapBox.getEditor().getItem();  // 「追加」を押し忘れたマップも入れる
        String pm = pending == null ? "" : pending.toString().trim();
        if (!pm.isEmpty() && !maps.contains(pm)) maps.addElement(pm);
        try {
            Integer lv = num(level, "レベル");
            Integer[] stats = {num(hp, "生命"), num(atkMin, "最小攻撃力"), num(atkMax, "最大攻撃力"), num(def, "防御力"),
                    num(defRate, "防御成功率"), num(atkRate, "攻撃成功率")};
            String iconRel = null;
            if (icon != null) {
                File dir = db.iconDir();
                dir.mkdirs();
                String fname = "manual_" + Integer.toHexString(n.hashCode()) + "_" + System.currentTimeMillis() + ".png";
                ImageIO.write(icon, "png", new File(dir, fname));
                iconRel = dir.getName() + "/" + fname;
            }
            List<String> ms = new ArrayList<>();
            for (int i = 0; i < maps.size(); i++) ms.add(maps.get(i));
            db.saveMonster(n, lv, ms, iconRel, note.getText().trim(), stats);
            result = n;
            dispose();  // 登録が終わったら閉じる
        } catch (IllegalArgumentException ex) {
            JOptionPane.showMessageDialog(this, ex.getMessage());
        } catch (Exception ex) {
            JOptionPane.showMessageDialog(this, "登録できません: " + ex.getMessage());
        }
    }

    /** モンスター登録画面を開く。登録したモンスターの名前を返す (キャンセルなら null)。 */
    public static String open(Window owner, MapDb db, String map) {
        MonsterDialog d = new MonsterDialog(owner, db, map);
        d.setVisible(true);
        return d.result;
    }
}
