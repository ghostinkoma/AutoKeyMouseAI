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
import javax.swing.JTabbedPane;
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
    private final SightingsPanel sightingsPanel;
    private final MapsPanel mapsPanel;
    private final JTabbedPane tabs = new JTabbedPane();
    private final JComboBox<String> mapBox = new JComboBox<>();
    private final JCheckBox follow = new JCheckBox("現在地のマップを表示", true);
    private final JCheckBox center = new JCheckBox("現在地を中央に", false);
    private final JLabel status = new JLabel(" ");
    private final JLabel hover = new JLabel(" ");
    private final DefaultListModel<String> events = new DefaultListModel<>();
    private final DefaultListModel<String> spotModel = new DefaultListModel<>();
    private List<MapDb.Spot> spotList = List.of();
    private final JList<String> spotView = new JList<>(spotModel);
    private MapDb.Pos lastPos;
    private java.util.Map<Long, List<String>> spotMonsters = java.util.Map.of();
    private String shownMap;
    private boolean fitted;
    private boolean updatingBox;

    public MapViewer(MapDb db) {
        super("MU Map Viewer - " + db.path);
        this.db = db;
        SightingsPanel.MapViewerContext ctx = new SightingsPanel.MapViewerContext() {
            @Override public MapDb db() { return MapViewer.this.db; }
            @Override public void changed() { refresh(); }
        };
        sightingsPanel = new SightingsPanel(ctx);
        mapsPanel = new MapsPanel(ctx);
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
        JButton here = new JButton("現在地を狩場に登録…");
        here.addActionListener(e -> addAtCurrent());
        top.add(here);
        JButton newMonster = new JButton("モンスター登録…");
        newMonster.addActionListener(e -> {
            if (MonsterDialog.open(this, db, shownMap) != null) refresh();
        });
        top.add(newMonster);
        JButton newMap = new JButton("マップ登録…");
        newMap.addActionListener(e -> {
            String m = MapsPanel.addManual(this, ctx);
            if (m != null) {
                follow.setSelected(false);
                refresh();
                selectMap(m);
                refresh();
            }
        });
        top.add(newMap);
        JButton open = new JButton("DB を開く…");
        open.addActionListener(e -> chooseDb());
        top.add(open);

        spotView.addMouseListener(new java.awt.event.MouseAdapter() {
            @Override public void mouseClicked(java.awt.event.MouseEvent e) {
                if (e.getClickCount() == 2) showSpot(selectedSpot());
            }
        });
        JPanel spotButtons = new JPanel(new FlowLayout(FlowLayout.LEFT, 4, 2));
        JButton bAdd = new JButton("追加…");
        bAdd.addActionListener(e -> addAtCurrent());
        JButton bEdit = new JButton("編集…");
        bEdit.addActionListener(e -> editSpot(selectedSpot()));
        JButton bDel = new JButton("削除");
        bDel.addActionListener(e -> deleteSpot(selectedSpot()));
        JButton bGo = new JButton("地図で見る");
        bGo.addActionListener(e -> showSpot(selectedSpot()));
        spotButtons.add(bAdd);
        spotButtons.add(bEdit);
        spotButtons.add(bDel);
        spotButtons.add(bGo);
        JPanel spotPane = new JPanel(new BorderLayout());
        spotPane.add(new JScrollPane(spotView), BorderLayout.CENTER);
        spotPane.add(spotButtons, BorderLayout.SOUTH);
        spotPane.setBorder(BorderFactory.createTitledBorder("登録地点 (狩場など)  ※地図の右クリックでも登録できます"));
        JPanel side = new JPanel(new BorderLayout());
        JPanel sp = spotPane;
        JScrollPane ep = new JScrollPane(new JList<>(events));
        tabs.addTab("目撃 (相手の名前)", sightingsPanel);
        tabs.addTab("新しいマップ", mapsPanel);
        tabs.addTab("出来事 (マップ移動・ワープ)", ep);
        JSplitPane sideSplit = new JSplitPane(JSplitPane.VERTICAL_SPLIT, sp, tabs);
        sideSplit.setResizeWeight(0.4);
        side.add(sideSplit, BorderLayout.CENTER);
        side.setPreferredSize(new Dimension(560, 600));

        JSplitPane split = new JSplitPane(JSplitPane.HORIZONTAL_SPLIT, panel, side);
        split.setResizeWeight(1.0);

        JPanel bottom = new JPanel(new BorderLayout());
        bottom.add(status, BorderLayout.WEST);
        bottom.add(hover, BorderLayout.EAST);
        bottom.setBorder(BorderFactory.createEmptyBorder(2, 6, 2, 6));

        add(top, BorderLayout.NORTH);
        add(split, BorderLayout.CENTER);
        add(bottom, BorderLayout.SOUTH);

        panel.onHover((x, y) -> {
            if (x < 0) {
                hover.setText(" ");
                return;
            }
            List<String> near = panel.sightingsNear(x, y);
            hover.setText(String.format("(%d, %d)  %s%s", x, y,
                    panel.visits(x, y) > 0 ? "歩けるマス 訪問 " + panel.visits(x, y) + " 回" : "未記録",
                    near.isEmpty() ? "" : "  見かけた: " + String.join(", ", near)));
        });
        panel.onContext(this::contextMenu);
        panel.onClick(this::showMonstersAt);

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
            panel.setSightings(db.sightingsOn(map, 3000));
            sightingsPanel.refresh();
            mapsPanel.refresh();
            int newMaps = db.pendingMaps();
            tabs.setTitleAt(1, newMaps > 0 ? "新しいマップ  候補 " + newMaps : "新しいマップ");
            int pending = db.pendingSightings();
            tabs.setTitleAt(0, pending > 0 ? "目撃 (相手の名前)  保留 " + pending : "目撃 (相手の名前)");
            if (center.isSelected() && cur != null && Objects.equals(cur.map(), map)) panel.centerOn(cur.x(), cur.y());

            lastPos = cur;
            List<MapDb.Spot> spots = db.spots(null);
            java.util.Map<Long, List<String>> mons = db.spotMonsters();
            if (!spots.equals(spotList) || !mons.equals(spotMonsters)) {
                spotMonsters = mons;  // 変わったときだけ作り直す (選択を保つ)
                long selId = selectedSpot() == null ? -1 : selectedSpot().id();
                spotList = spots;
                spotModel.clear();
                for (MapDb.Spot s : spotList)
                    spotModel.addElement(String.format("#%d [%s] %s  %s (%d, %d) 半径%d%s%s", s.id(), SpotDialog.kindLabel(s.kind()),
                            s.name(), s.map(), s.x(), s.y(), s.radius(),
                            spotMonsters.containsKey(s.id()) ? "  狙い: " + String.join(", ", spotMonsters.get(s.id())) : "",
                            s.note() == null || s.note().isEmpty() ? "" : "  " + s.note()));
                for (int i = 0; i < spotList.size(); i++) if (spotList.get(i).id() == selId) spotView.setSelectedIndex(i);
            }
            events.clear();
            for (MapDb.Event e : db.events(200)) {
                String where = e.map() == null ? "" : e.map() + (e.x() == null ? "" : " (" + e.x() + "," + e.y() + ")");
                events.addElement(TIME.format(new Date((long) (e.ts() * 1000))) + "  " + e.kind() + "  " + where
                        + (e.detail() == null || e.detail().isEmpty() ? "" : "  " + e.detail()));
            }
            status.setText(cur == null ? "まだ位置の記録がありません (python tools\\mirror_only.py --recognize か tools\\map_logger.py を実行してください)"
                    : String.format("現在地: %s (%d, %d)  %s 更新   表示中: %s", cur.map(), cur.x(), cur.y(),
                    TIME.format(new Date((long) (cur.ts() * 1000))), map));
        } catch (SQLException ex) {
            status.setText("DB を読めません: " + ex.getMessage());
        }
    }

    /** 地図をクリック (タップ) した場所の近くで見かけたモンスターを出す。 */
    private void showMonstersAt(int x, int y, java.awt.event.MouseEvent e) {
        if (shownMap == null) return;
        int r = 5;  // 10 マス四方 (10 マス以上離れると出現モンスターが変わる)
        panel.mark(x, y, r);
        JPopupMenu menu = new JPopupMenu();
        JLabel title = new JLabel(String.format("  %s (%d, %d) 付近 %d マス四方で見かけたモンスター", shownMap, x, y, 2 * r));
        title.setFont(title.getFont().deriveFont(java.awt.Font.BOLD));
        menu.add(title);
        menu.addSeparator();
        try {
            List<MapDb.NearMonster> near = db.monstersNear(shownMap, x, y, r);
            if (near.isEmpty()) menu.add(new JLabel("  (まだ記録がありません)"));
            for (MapDb.NearMonster m : near) {
                JMenuItem it = new JMenuItem(m.label() + "  最後 " + TIME.format(new Date((long) (m.lastTs() * 1000))));
                it.setToolTipText("クリックでモンスター登録画面 (アイコン・ステータスを入れられる)");
                it.addActionListener(a -> {
                    if (MonsterDialog.open(this, db, shownMap, m.name(), m.minLevel()) != null) refresh();
                });
                menu.add(it);
            }
            for (MapDb.Spot s : spotList) {
                if (!s.map().equals(shownMap) || Math.hypot(s.x() - x, s.y() - y) > Math.max(2, s.radius())) continue;
                List<String> target = spotMonsters.get(s.id());
                if (target != null && !target.isEmpty())
                    menu.add(new JLabel("  狩場「" + s.name() + "」の狙い: " + String.join(", ", target)));
            }
        } catch (SQLException ex) {
            menu.add(new JLabel("  DB を読めません: " + ex.getMessage()));
        }
        menu.addSeparator();
        JMenuItem reg = new JMenuItem("このマップに新しいモンスターを登録…");
        reg.addActionListener(a -> {
            if (MonsterDialog.open(this, db, shownMap) != null) refresh();
        });
        menu.add(reg);
        JMenuItem spot = new JMenuItem(String.format("ここ (%d, %d) を狩場などに登録…", x, y));
        spot.addActionListener(a -> addSpot(shownMap, x, y));
        menu.add(spot);
        menu.addPopupMenuListener(new javax.swing.event.PopupMenuListener() {
            @Override public void popupMenuWillBecomeVisible(javax.swing.event.PopupMenuEvent ev) {}
            @Override public void popupMenuWillBecomeInvisible(javax.swing.event.PopupMenuEvent ev) { panel.mark(0, 0, -1); }
            @Override public void popupMenuCanceled(javax.swing.event.PopupMenuEvent ev) {}
        });
        menu.show(e.getComponent(), e.getX(), e.getY());
    }

    private void contextMenu(int x, int y, java.awt.event.MouseEvent e) {
        if (shownMap == null) return;
        JPopupMenu menu = new JPopupMenu();
        JMenuItem add = new JMenuItem(String.format("ここ (%d, %d) を狩場などに登録…", x, y));
        add.addActionListener(a -> addSpot(shownMap, x, y));
        menu.add(add);
        for (MapDb.Spot s : spotList) {
            if (!s.map().equals(shownMap) || Math.hypot(s.x() - x, s.y() - y) > Math.max(2, s.radius())) continue;
            menu.addSeparator();
            JMenuItem ed = new JMenuItem("「" + s.name() + "」を編集…");
            ed.addActionListener(a -> editSpot(s));
            menu.add(ed);
            JMenuItem mv = new JMenuItem("「" + s.name() + "」の中心をここ (" + x + ", " + y + ") に移す");
            mv.addActionListener(a -> {
                try {
                    db.updateSpot(s.id(), s.name(), s.map(), x, y, s.radius(), s.kind(), s.note());
                    refresh();
                } catch (SQLException ex) {
                    error("変更できません", ex);
                }
            });
            menu.add(mv);
            JMenuItem del = new JMenuItem("「" + s.name() + "」を削除");
            del.addActionListener(a -> deleteSpot(s));
            menu.add(del);
        }
        menu.show(e.getComponent(), e.getX(), e.getY());
    }

    private MapDb.Spot selectedSpot() {
        int i = spotView.getSelectedIndex();
        return i >= 0 && i < spotList.size() ? spotList.get(i) : null;
    }

    private List<MapDb.Monster> monstersOf(String map) {
        try {
            return db.monsters(map);
        } catch (SQLException e) {
            return List.of();
        }
    }

    private List<String> mapNames() {
        try {
            return db.maps();
        } catch (SQLException e) {
            return List.of();
        }
    }

    /** 今いる場所 (map_logger.py の最新の位置) を登録する。位置がまだ無ければ表示中のマップの中央。 */
    private void addAtCurrent() {
        if (lastPos != null) addSpot(lastPos.map(), lastPos.x(), lastPos.y());
        else addSpot(shownMap, 128, 128);
    }

    private void addSpot(String map, int x, int y) {
        SpotDialog.Result r = SpotDialog.create(this, mapNames(), this::monstersOf,
                (owner, m) -> MonsterDialog.open(owner, db, m), map, x, y);
        if (r == null) return;
        try {
            long id = db.addSpot(r.name(), r.map(), r.x(), r.y(), r.radius(), r.kind(), r.note());
            db.setSpotMonsters(id, r.monsters());
            status.setText("登録しました: " + r.name() + "  " + r.map() + " (" + r.x() + ", " + r.y() + ")");
            refresh();
        } catch (SQLException ex) {
            error("登録できません", ex);
        }
    }

    private void editSpot(MapDb.Spot s) {
        if (s == null) {
            JOptionPane.showMessageDialog(this, "一覧から地点を選んでください");
            return;
        }
        SpotDialog.Result r = SpotDialog.edit(this, mapNames(), this::monstersOf,
                (owner, m) -> MonsterDialog.open(owner, db, m), s,
                spotMonsters.getOrDefault(s.id(), List.of()));
        if (r == null) return;
        try {
            db.updateSpot(s.id(), r.name(), r.map(), r.x(), r.y(), r.radius(), r.kind(), r.note());
            db.setSpotMonsters(s.id(), r.monsters());
            refresh();
        } catch (SQLException ex) {
            error("保存できません", ex);
        }
    }

    private void deleteSpot(MapDb.Spot s) {
        if (s == null) {
            JOptionPane.showMessageDialog(this, "一覧から地点を選んでください");
            return;
        }
        if (JOptionPane.showConfirmDialog(this, "「" + s.name() + "」を削除しますか?", "削除",
                JOptionPane.OK_CANCEL_OPTION) != JOptionPane.OK_OPTION) return;
        try {
            db.deleteSpot(s.id());
            refresh();
        } catch (SQLException ex) {
            error("削除できません", ex);
        }
    }

    private void showSpot(MapDb.Spot s) {
        if (s == null) return;
        follow.setSelected(false);
        selectMap(s.map());
        refresh();
        panel.centerOn(s.x(), s.y());
    }

    private void error(String what, Exception ex) {
        JOptionPane.showMessageDialog(this, what + ": " + ex.getMessage());
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
