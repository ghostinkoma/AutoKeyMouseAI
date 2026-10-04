package akm.viewer;

import java.awt.BorderLayout;
import java.awt.Dimension;
import java.awt.FlowLayout;
import java.awt.image.BufferedImage;
import java.io.File;
import java.sql.SQLException;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import java.util.Objects;
import java.util.prefs.Preferences;
import javax.imageio.ImageIO;
import javax.swing.BorderFactory;
import javax.swing.DefaultListModel;
import javax.swing.JButton;
import javax.swing.JCheckBox;
import javax.swing.JComboBox;
import javax.swing.JFileChooser;
import javax.swing.JFrame;
import javax.swing.JLabel;
import javax.swing.JList;
import javax.swing.JMenuItem;
import javax.swing.JOptionPane;
import javax.swing.JPanel;
import javax.swing.JPopupMenu;
import javax.swing.JScrollPane;
import javax.swing.JSplitPane;
import javax.swing.SwingUtilities;
import javax.swing.Timer;
import javax.swing.UIManager;
import javax.swing.filechooser.FileNameExtensionFilter;

/**
 * MU マップビューア。Python の map_logger.py が記録する SQLite (pc/data/mu_map.db) を 1 秒ごとに読み直して表示する。
 *
 * <pre>
 *   java -jar mapviewer.jar                       (DB を自動で探す / 選ぶ)
 *   java -jar mapviewer.jar D:\...\pc\data\mu_map.db
 *   java -jar mapviewer.jar --render mu_map.db Atlans out.png   (画像に書き出す)
 * </pre>
 */
public final class MapViewer extends JFrame {
    private static final Preferences PREFS = Preferences.userNodeForPackage(MapViewer.class);
    private static final SimpleDateFormat TIME = new SimpleDateFormat("HH:mm:ss");

    private MapDb db;
    private final MapPanel panel = new MapPanel();
    private final JComboBox<String> mapBox = new JComboBox<>();
    private final JCheckBox follow = new JCheckBox("現在地のマップを表示", true);
    private final JCheckBox center = new JCheckBox("現在地を中央に", false);
    private final JLabel status = new JLabel(" ");
    private final JLabel hover = new JLabel(" ");
    private final DefaultListModel<String> events = new DefaultListModel<>();
    private final DefaultListModel<String> spotModel = new DefaultListModel<>();
    private List<MapDb.Spot> spotList = List.of();
    private String shownMap;
    private boolean fitted;
    private boolean updatingBox;

    public MapViewer(MapDb db) {
        super("MU Map Viewer - " + db.path);
        this.db = db;
        setDefaultCloseOperation(EXIT_ON_CLOSE);

        JPanel top = new JPanel(new FlowLayout(FlowLayout.LEFT));
        top.add(new JLabel("マップ:"));
        mapBox.setPreferredSize(new Dimension(200, mapBox.getPreferredSize().height));
        mapBox.addActionListener(e -> {
            if (updatingBox) return;
            follow.setSelected(false);
            fitted = false;
            refresh();
        });
        top.add(mapBox);
        top.add(follow);
        top.add(center);
        JButton fit = new JButton("全体表示");
        fit.addActionListener(e -> panel.fit());
        top.add(fit);
        JButton open = new JButton("DB を開く…");
        open.addActionListener(e -> chooseDb());
        top.add(open);

        JList<String> spotView = new JList<>(spotModel);
        spotView.addMouseListener(new java.awt.event.MouseAdapter() {
            @Override public void mouseClicked(java.awt.event.MouseEvent e) {
                int i = spotView.getSelectedIndex();
                if (e.getClickCount() == 2 && i >= 0 && i < spotList.size()) {
                    MapDb.Spot s = spotList.get(i);
                    follow.setSelected(false);
                    selectMap(s.map());
                    refresh();
                    panel.centerOn(s.x(), s.y());
                }
            }
        });
        JPanel side = new JPanel(new BorderLayout());
        JScrollPane sp = new JScrollPane(spotView);
        sp.setBorder(BorderFactory.createTitledBorder("登録地点 (ダブルクリックで移動 / 地図を右クリックで追加・削除)"));
        JScrollPane ep = new JScrollPane(new JList<>(events));
        ep.setBorder(BorderFactory.createTitledBorder("出来事 (マップ移動・ワープ)"));
        JSplitPane sideSplit = new JSplitPane(JSplitPane.VERTICAL_SPLIT, sp, ep);
        sideSplit.setResizeWeight(0.4);
        side.add(sideSplit, BorderLayout.CENTER);
        side.setPreferredSize(new Dimension(330, 600));

        JSplitPane split = new JSplitPane(JSplitPane.HORIZONTAL_SPLIT, panel, side);
        split.setResizeWeight(1.0);

        JPanel bottom = new JPanel(new BorderLayout());
        bottom.add(status, BorderLayout.WEST);
        bottom.add(hover, BorderLayout.EAST);
        bottom.setBorder(BorderFactory.createEmptyBorder(2, 6, 2, 6));

        add(top, BorderLayout.NORTH);
        add(split, BorderLayout.CENTER);
        add(bottom, BorderLayout.SOUTH);

        panel.onHover((x, y) -> hover.setText(x < 0 ? " " :
                String.format("(%d, %d)  %s", x, y, panel.visits(x, y) > 0 ? "歩けるマス 訪問 " + panel.visits(x, y) + " 回" : "未記録")));
        panel.onContext(this::contextMenu);

        setSize(1200, 860);
        setLocationRelativeTo(null);
        new Timer(1000, e -> refresh()).start();
        SwingUtilities.invokeLater(this::refresh);
    }

    private void selectMap(String map) {
        updatingBox = true;
        mapBox.setSelectedItem(map);
        updatingBox = false;
    }

    private void refresh() {
        try {
            List<String> maps = db.maps();
            MapDb.Pos cur = db.latest();
            updatingBox = true;
            Object sel = mapBox.getSelectedItem();
            if (mapBox.getItemCount() != maps.size()) {
                mapBox.removeAllItems();
                for (String m : maps) mapBox.addItem(m);
                if (sel != null) mapBox.setSelectedItem(sel);
            }
            updatingBox = false;
            String map = (String) mapBox.getSelectedItem();
            if (follow.isSelected() && cur != null) {
                map = cur.map();
                selectMap(map);
            }
            if (!Objects.equals(map, shownMap)) fitted = false;
            shownMap = map;

            panel.setData(db.cells(map), db.trail(map, 300), db.spots(map),
                    cur != null && Objects.equals(cur.map(), map) ? cur : null);
            if (!fitted && panel.getWidth() > 0) {
                panel.fit();
                fitted = true;
            }
            if (center.isSelected() && cur != null && Objects.equals(cur.map(), map)) panel.centerOn(cur.x(), cur.y());

            spotList = db.spots(null);
            spotModel.clear();
            for (MapDb.Spot s : spotList)
                spotModel.addElement(String.format("#%d %s  %s (%d,%d) r%d [%s]", s.id(), s.name(), s.map(), s.x(), s.y(), s.radius(), s.kind()));
            events.clear();
            for (MapDb.Event e : db.events(200)) {
                String where = e.map() == null ? "" : e.map() + (e.x() == null ? "" : " (" + e.x() + "," + e.y() + ")");
                events.addElement(TIME.format(new Date((long) (e.ts() * 1000))) + "  " + e.kind() + "  " + where
                        + (e.detail() == null || e.detail().isEmpty() ? "" : "  " + e.detail()));
            }
            status.setText(cur == null ? "まだ位置の記録がありません (python tools\\map_logger.py を実行してください)"
                    : String.format("現在地: %s (%d, %d)  %s 更新   表示中: %s", cur.map(), cur.x(), cur.y(),
                    TIME.format(new Date((long) (cur.ts() * 1000))), map));
        } catch (SQLException ex) {
            status.setText("DB を読めません: " + ex.getMessage());
        }
    }

    private void contextMenu(int x, int y, java.awt.event.MouseEvent e) {
        if (shownMap == null) return;
        JPopupMenu menu = new JPopupMenu();
        JMenuItem add = new JMenuItem(String.format("ここ (%d, %d) を地点として登録…", x, y));
        add.addActionListener(a -> {
            String name = JOptionPane.showInputDialog(this, "名前 (例: Atlans 狩場1)", shownMap + " " + x + "," + y);
            if (name == null || name.isBlank()) return;
            String[] kinds = {"hunt", "shop", "town", "safe", "other"};
            Object kind = JOptionPane.showInputDialog(this, "種類 (hunt = 狩場)", "種類", JOptionPane.QUESTION_MESSAGE,
                    null, kinds, kinds[0]);
            if (kind == null) return;
            String r = JOptionPane.showInputDialog(this, "半径 (マス)", "5");
            if (r == null) return;
            try {
                db.addSpot(name.trim(), shownMap, x, y, Integer.parseInt(r.trim()), kind.toString(), "");
                refresh();
            } catch (NumberFormatException | SQLException ex) {
                JOptionPane.showMessageDialog(this, "登録できません: " + ex.getMessage());
            }
        });
        menu.add(add);
        for (MapDb.Spot s : spotList) {
            if (!s.map().equals(shownMap) || Math.hypot(s.x() - x, s.y() - y) > Math.max(2, s.radius())) continue;
            JMenuItem del = new JMenuItem("地点「" + s.name() + "」を削除");
            del.addActionListener(a -> {
                try {
                    db.deleteSpot(s.id());
                    refresh();
                } catch (SQLException ex) {
                    JOptionPane.showMessageDialog(this, "削除できません: " + ex.getMessage());
                }
            });
            menu.add(del);
        }
        menu.show(e.getComponent(), e.getX(), e.getY());
    }

    private void chooseDb() {
        File f = pickDb(this);
        if (f == null) return;
        try {
            MapDb next = new MapDb(f.getAbsolutePath());
            db.close();
            db = next;
            PREFS.put("db", f.getAbsolutePath());
            setTitle("MU Map Viewer - " + f.getAbsolutePath());
            fitted = false;
            refresh();
        } catch (SQLException ex) {
            JOptionPane.showMessageDialog(this, "開けません: " + ex.getMessage());
        }
    }

    private static File pickDb(java.awt.Component parent) {
        JFileChooser fc = new JFileChooser(PREFS.get("db", "."));
        fc.setDialogTitle("map_logger.py の記録 (mu_map.db) を選んでください");
        fc.setFileFilter(new FileNameExtensionFilter("SQLite (*.db)", "db"));
        return fc.showOpenDialog(parent) == JFileChooser.APPROVE_OPTION ? fc.getSelectedFile() : null;
    }

    /** DB の場所: 引数 → 前回の場所 → よくある場所 → 選択ダイアログ。 */
    private static File findDb(String[] args) {
        if (args.length > 0) return new File(args[0]);
        String last = PREFS.get("db", null);
        if (last != null && new File(last).exists()) return new File(last);
        for (String p : new String[] {"data/mu_map.db", "pc/data/mu_map.db", "../pc/data/mu_map.db", "../../pc/data/mu_map.db"}) {
            File f = new File(p);
            if (f.exists()) return f;
        }
        return pickDb(null);
    }

    /** 画面を出さずに PNG に書き出す (確認用)。 */
    private static void render(String dbPath, String map, String out) throws Exception {
        try (MapDb db = new MapDb(dbPath)) {
            MapPanel p = new MapPanel();
            p.setSize(800, 800);
            MapDb.Pos cur = db.latest();
            p.setData(db.cells(map), db.trail(map, 300), db.spots(map), cur != null && map.equals(cur.map()) ? cur : null);
            p.fit();
            BufferedImage img = new BufferedImage(800, 800, BufferedImage.TYPE_INT_RGB);
            p.paint(img.getGraphics());
            ImageIO.write(img, "png", new File(out));
        }
    }

    public static void main(String[] args) throws Exception {
        if (args.length == 4 && args[0].equals("--render")) {
            render(args[1], args[2], args[3]);
            return;
        }
        try {
            UIManager.setLookAndFeel(UIManager.getSystemLookAndFeelClassName());
        } catch (Exception ignored) {
        }
        File f = findDb(args);
        if (f == null) return;
        if (!f.exists()) {
            JOptionPane.showMessageDialog(null, "ファイルがありません: " + f.getAbsolutePath()
                    + "\n先に python tools\\map_logger.py で記録してください");
            return;
        }
        PREFS.put("db", f.getAbsolutePath());
        MapDb db = new MapDb(f.getAbsolutePath());
        SwingUtilities.invokeLater(() -> new MapViewer(db).setVisible(true));
    }
}
