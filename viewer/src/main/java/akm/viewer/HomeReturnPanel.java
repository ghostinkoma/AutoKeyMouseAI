package akm.viewer;

import java.awt.BorderLayout;
import java.awt.FlowLayout;
import java.awt.GridBagConstraints;
import java.awt.GridBagLayout;
import java.awt.Insets;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.LinkedHashMap;
import java.util.Map;
import javax.swing.BorderFactory;
import javax.swing.JButton;
import javax.swing.JCheckBox;
import javax.swing.JComponent;
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.JScrollPane;
import javax.swing.JSpinner;
import javax.swing.JTextField;
import javax.swing.SpinnerNumberModel;

/**
 * MU Helper の帰還の設定と状態。学習ループ (pc/akm/home_return.py) と次のファイルでやり取りする:
 * <ul>
 *   <li>設定 (ここで書く): pc/models/home_return.properties</li>
 *   <li>状態 (学習ループが書く): pc/models/home_return_state.properties</li>
 * </ul>
 * 「保存」を押すと、学習ループは数秒以内に読み直す (起動し直さなくてよい)。
 */
public final class HomeReturnPanel extends JPanel {
    private final SightingsPanel.MapViewerContext ctx;
    private final JCheckBox enabled = new JCheckBox("帰還する");
    private final JSpinner interval = spinner(120, 10, 3600, 10);
    private final JSpinner minDist = spinner(0, 0, 50, 1);
    private final JSpinner offWait = spinner(100, 0, 3000, 50);
    private final JSpinner arrive = spinner(1, 1, 10, 1);
    private final JSpinner maxWalk = spinner(20, 2, 120, 1);
    private final JTextField potionKey = new JTextField("q", 6);
    private final JSpinner potionHp = spinner(50, 0, 100, 5);
    private final JSpinner potionEvery = spinner(400, 50, 5000, 50);
    private final JTextField helperKey = new JTextField("home", 6);
    private final JCheckBox restore = new JCheckBox("終わったら元の窓に戻す", true);
    private final JCheckBox keepOn = new JCheckBox("MU Helper を常にオンに保つ (AI で確かめて入れる)", true);
    private final JSpinner keepOnS = spinner(10, 3, 600, 1);
    private final JTextField homeMap = new JTextField(8);
    private final JSpinner homeX = spinner(0, 0, 255, 1);
    private final JSpinner homeY = spinner(0, 0, 255, 1);
    private final javax.swing.JTextArea state = new javax.swing.JTextArea(5, 40);
    private final JLabel saved = new JLabel(" ");
    private long stateTime = -1;
    private int overrideSeq = 0;
    private boolean loaded;

    public HomeReturnPanel(SightingsPanel.MapViewerContext ctx) {
        super(new BorderLayout());
        this.ctx = ctx;
        JPanel form = new JPanel(new GridBagLayout());
        int[] row = {0};
        addRow(form, row, "", enabled);
        addRow(form, row, "間隔 (秒)", interval);
        addRow(form, row, "近ければ何もしない (マス)", minDist);
        addRow(form, row, "切ってから待つ (ms)", offWait);
        addRow(form, row, "着いた距離 (マス)", arrive);
        addRow(form, row, "あきらめる (秒)", maxWalk);
        addRow(form, row, "ポーションのキー", potionKey);
        addRow(form, row, "ポーション HP (%)", potionHp);
        addRow(form, row, "ポーション間隔 (ms)", potionEvery);
        addRow(form, row, "Helper のキー", helperKey);
        addRow(form, row, "", restore);
        addRow(form, row, "", keepOn);
        addRow(form, row, "止まっていたら入れるまで (秒)", keepOnS);

        JPanel homeRow = new JPanel(new FlowLayout(FlowLayout.LEFT, 4, 0));
        homeRow.add(new JLabel("マップ"));
        homeRow.add(homeMap);
        homeRow.add(new JLabel("x"));
        homeRow.add(homeX);
        homeRow.add(new JLabel("y"));
        homeRow.add(homeY);
        addRow(form, row, "帰る場所", homeRow);
        JPanel homeButtons = new JPanel(new FlowLayout(FlowLayout.LEFT, 4, 0));
        JButton here = new JButton("現在地を入れる");
        here.addActionListener(e -> fillCurrent());
        JButton setHome = new JButton("この場所にする");
        setHome.addActionListener(e -> save(true));
        homeButtons.add(here);
        homeButtons.add(setHome);
        addRow(form, row, "", homeButtons);
        state.setEditable(false);
        state.setLineWrap(true);
        state.setOpaque(false);
        state.setText("-");

        JButton save = new JButton("保存");
        save.addActionListener(e -> save(false));
        JPanel bottom = new JPanel(new FlowLayout(FlowLayout.LEFT));
        bottom.add(save);
        bottom.add(saved);

        JPanel stateBox = new JPanel(new BorderLayout());
        stateBox.add(new JScrollPane(state), BorderLayout.CENTER);
        stateBox.setBorder(BorderFactory.createTitledBorder("今の状態 (学習ループが書く)"));
        JPanel south = new JPanel(new BorderLayout());
        south.add(stateBox, BorderLayout.CENTER);
        south.add(bottom, BorderLayout.SOUTH);
        add(new JScrollPane(form), BorderLayout.CENTER);
        add(south, BorderLayout.SOUTH);
        setBorder(BorderFactory.createTitledBorder(
                "MU Helper の帰還 (手で開始した場所へ戻る)"));
    }

    private static JSpinner spinner(int v, int min, int max, int step) {
        return new JSpinner(new SpinnerNumberModel(v, min, max, step));
    }

    private static void addRow(JPanel p, int[] row, String label, JComponent c) {
        GridBagConstraints g = new GridBagConstraints();
        g.gridy = row[0]++;
        g.insets = new Insets(1, 4, 1, 4);
        g.anchor = GridBagConstraints.WEST;
        g.gridx = 0;
        p.add(new JLabel(label), g);
        g.gridx = 1;
        p.add(c, g);
    }

    private Path dir() {
        return WindowsPanel.modelsDir(ctx.db().path);
    }

    /** key=value の読み込み (pc/akm/home_return.py の load_props と同じ書き方)。 */
    static Map<String, String> readProps(Path f) {
        Map<String, String> out = new LinkedHashMap<>();
        try {
            for (String l : Files.readAllLines(f, StandardCharsets.UTF_8)) {
                l = l.strip();
                if (l.isEmpty() || l.startsWith("#") || !l.contains("=")) continue;
                int i = l.indexOf('=');
                out.put(l.substring(0, i).strip(), l.substring(i + 1).strip());
            }
        } catch (IOException ex) {
            // 無ければ既定のまま
        }
        return out;
    }

    private static int num(Map<String, String> p, String k, int def) {
        try {
            return (int) Math.round(Double.parseDouble(p.getOrDefault(k, "")));
        } catch (NumberFormatException ex) {
            return def;
        }
    }

    private void load() {
        Map<String, String> p = readProps(dir().resolve("home_return.properties"));
        enabled.setSelected("true".equalsIgnoreCase(p.getOrDefault("enabled", "false")));
        interval.setValue(num(p, "interval_s", 120));
        minDist.setValue(num(p, "min_distance", 0));
        offWait.setValue(num(p, "off_wait_ms", 100));
        arrive.setValue(Math.max(1, num(p, "arrive_tiles", 1)));
        maxWalk.setValue(num(p, "max_walk_s", 20));
        potionKey.setText(p.getOrDefault("potion_key", "q"));
        double hp = 0.5;
        try {
            hp = Double.parseDouble(p.getOrDefault("potion_hp", "0.5"));
        } catch (NumberFormatException ex) {
            // 既定
        }
        potionHp.setValue((int) Math.round(hp * 100));
        potionEvery.setValue(num(p, "potion_every_ms", 400));
        helperKey.setText(p.getOrDefault("helper_key", "home"));
        restore.setSelected(!"false".equalsIgnoreCase(p.getOrDefault("restore_focus", "true")));
        keepOn.setSelected(!"false".equalsIgnoreCase(p.getOrDefault("keep_on", "true")));
        keepOnS.setValue(num(p, "keep_on_s", 10));
        overrideSeq = num(p, "override_seq", 0);
        String[] h = p.getOrDefault("home_override", "").split(",");
        if (h.length == 3) {
            homeMap.setText(h[0]);
            homeX.setValue(num(Map.of("v", h[1]), "v", 0));
            homeY.setValue(num(Map.of("v", h[2]), "v", 0));
        }
    }

    private void fillCurrent() {
        try {
            MapDb.Pos p = ctx.db().latest();
            if (p == null) {
                saved.setText("現在地がまだ記録されていません");
                return;
            }
            homeMap.setText(p.map());
            homeX.setValue(p.x());
            homeY.setValue(p.y());
            saved.setText("現在地を入れました。「この場所にする」で帰る場所になります");
        } catch (Exception ex) {
            saved.setText("現在地を読めません: " + ex.getMessage());
        }
    }

    /** 設定を書く。setHome なら入力した場所を帰る場所にする (override_seq を増やして学習ループに知らせる)。 */
    private void save(boolean setHome) {
        String home = homeMap.getText().strip();
        if (setHome) {
            if (home.isEmpty() || home.contains(",")) {
                saved.setText("マップ名を入れてください (「現在地を入れる」でも)");
                return;
            }
            overrideSeq++;
        }
        Map<String, String> p = new LinkedHashMap<>();
        p.put("enabled", String.valueOf(enabled.isSelected()));
        p.put("interval_s", String.valueOf(interval.getValue()));
        p.put("min_distance", String.valueOf(minDist.getValue()));
        p.put("off_wait_ms", String.valueOf(offWait.getValue()));
        p.put("arrive_tiles", String.valueOf(arrive.getValue()));
        p.put("max_walk_s", String.valueOf(maxWalk.getValue()));
        p.put("potion_key", potionKey.getText().strip().toLowerCase());
        p.put("potion_hp", String.valueOf(((Integer) potionHp.getValue()) / 100.0));
        p.put("potion_every_ms", String.valueOf(potionEvery.getValue()));
        p.put("helper_key", helperKey.getText().strip().toLowerCase());
        p.put("restore_focus", String.valueOf(restore.isSelected()));
        p.put("keep_on", String.valueOf(keepOn.isSelected()));
        p.put("keep_on_s", String.valueOf(keepOnS.getValue()));
        p.put("home_override", home.isEmpty() ? "" : home + "," + homeX.getValue() + "," + homeY.getValue());
        p.put("override_seq", String.valueOf(overrideSeq));
        StringBuilder sb = new StringBuilder("# MU Helper の帰還の設定 (地図ビューアが書く。学習ループが読む)\n");
        p.forEach((k, v) -> sb.append(k).append('=').append(v).append('\n'));
        Path f = dir().resolve("home_return.properties");
        try {
            Files.createDirectories(f.getParent());
            Path tmp = f.resolveSibling("home_return.properties.tmp");
            Files.writeString(tmp, sb.toString(), StandardCharsets.UTF_8);
            Files.move(tmp, f, StandardCopyOption.REPLACE_EXISTING);
            saved.setText(setHome ? "帰る場所を " + home + " (" + homeX.getValue() + ", " + homeY.getValue() + ") にしました"
                    : "保存しました (学習ループが数秒以内に読み直します)");
        } catch (IOException ex) {
            saved.setText("保存できません: " + ex.getMessage());
        }
    }

    public void refresh() {
        if (!loaded) {
            load();
            loaded = true;
        }
        Path f = dir().resolve("home_return_state.properties");
        long t;
        try {
            t = Files.exists(f) ? Files.getLastModifiedTime(f).toMillis() : 0;
        } catch (IOException ex) {
            t = 0;
        }
        if (t == stateTime) return;
        stateTime = t;
        if (t == 0) {
            state.setText("学習ループが動いていません (start_all.ps1 / start_learn.ps1 で起動すると、ここに状態が出ます)");
            return;
        }
        Map<String, String> s = readProps(f);
        String home = s.getOrDefault("home", "");
        String src = "viewer".equals(s.get("home_src")) ? "ビューアで指定" : "MU Helper を手で開始した場所";
        boolean on = enabled.isSelected();
        StringBuilder sb = new StringBuilder();
        sb.append("帰る場所: ").append(home.isEmpty() ? "まだありません (MU Helper を手で開始すると記録します)"
                : home.replace(",", " ") + "  (" + src + ")").append('\n');
        sb.append("状態: ").append(s.getOrDefault("status", "-"))
                .append(on ? "  次の帰還まで " + s.getOrDefault("next_in_s", "?") + " 秒" : "  (帰還はオフ)")
                .append("  済 ").append(s.getOrDefault("cycles", "0")).append(" 回");
        if (!s.getOrDefault("here", "").isEmpty()) sb.append("  現在地 ").append(s.get("here").replace(",", " "));
        sb.append('\n');
        if ("no".equals(s.get("calibrated"))) sb.append("画面の向き: 最初の帰還のときに自動で調べます\n");
        if (!s.getOrDefault("helper", "").isEmpty()) sb.append("MU Helper (AI で確認): ").append(s.get("helper"));
        if ("yes".equals(s.get("manual_off"))) sb.append("  手で止めたので自動では入れません (手で開始すると戻ります)");
        if (!s.getOrDefault("helper", "").isEmpty() || "yes".equals(s.get("manual_off"))) sb.append('\n');
        sb.append("前回の帰還: ").append(s.getOrDefault("last_result", "").isEmpty() ? "-" : s.get("last_result"));
        state.setText(sb.toString());
    }
}
