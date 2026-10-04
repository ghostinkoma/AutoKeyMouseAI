package akm.viewer;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;

/** Python の map_logger.py が書き込む SQLite (mu_map.db) を読む。地点 (spots) の追加・削除だけ書き込む。 */
public final class MapDb implements AutoCloseable {
    public static final int SIZE = 256; // MU のマップは 256x256 マス

    public record Pos(String map, int x, int y, double ts) {}
    public record Spot(long id, String name, String map, int x, int y, int radius, String kind, String note) {}
    /** icon はアイコン画像の絶対パス (無ければ null)。 */
    public record Monster(String name, Integer level, String map, String icon, String note) {
        public String label(boolean withMap) {
            return name + " (Lv" + (level == null ? "?" : level) + (withMap && map != null && !map.isEmpty() ? ", " + map : "") + ")"
                    + (note == null || note.isEmpty() ? "" : " ※" + note);
        }
    }
    public record Event(double ts, String kind, String map, Integer x, Integer y, String detail) {}

    private final Connection conn;
    public final String path;

    public MapDb(String path) throws SQLException {
        this.path = path;
        conn = DriverManager.getConnection("jdbc:sqlite:" + path);
        try (Statement st = conn.createStatement()) {
            st.execute("PRAGMA busy_timeout=3000"); // Python が書き込み中なら少し待つ
        }
        try {
            ensureBuiltinMonsters();
        } catch (Exception e) {
            System.err.println("モンスター一覧を入れられません: " + e);
        }
    }

    /** モンスター一覧が空なら jar に同梱の一覧 (無[MU]脳2014) を入れる。狩場登録のドロップダウンに最初から出るように。 */
    public int ensureBuiltinMonsters() throws Exception {
        try (Statement st = conn.createStatement()) {
            st.execute("CREATE TABLE IF NOT EXISTS monsters (id INTEGER PRIMARY KEY, name TEXT NOT NULL, level INTEGER, "
                    + "map TEXT NOT NULL DEFAULT '', map_en TEXT NOT NULL DEFAULT '', source TEXT, updated REAL, icon TEXT, "
                    + "note TEXT, hp INTEGER, UNIQUE(name, map))");
            for (String[] c : new String[][] {{"icon", "TEXT"}, {"note", "TEXT"}, {"hp", "INTEGER"},
                    {"atk_min", "INTEGER"}, {"atk_max", "INTEGER"}, {"def", "INTEGER"}, {"def_rate", "INTEGER"}, {"atk_rate", "INTEGER"}})
                if (!hasColumn("monsters", c[0])) st.execute("ALTER TABLE monsters ADD COLUMN " + c[0] + " " + c[1]);
            try (ResultSet rs = st.executeQuery("SELECT COUNT(*) FROM monsters")) {
                if (rs.next() && rs.getInt(1) > 0) return 0;
            }
        }
        java.io.InputStream in = MapDb.class.getResourceAsStream("monsters.tsv");
        if (in == null) return 0;
        int n = 0;
        boolean auto = conn.getAutoCommit();
        conn.setAutoCommit(false);
        try (java.io.BufferedReader r = new java.io.BufferedReader(new java.io.InputStreamReader(in, java.nio.charset.StandardCharsets.UTF_8));
             PreparedStatement ps = conn.prepareStatement(
                     "INSERT OR IGNORE INTO monsters(name, level, map, map_en, source, updated, note, hp) VALUES (?,?,?,?,?,?,?,?)")) {
            String line;
            double now = System.currentTimeMillis() / 1000.0;
            while ((line = r.readLine()) != null) {
                if (line.isBlank() || line.startsWith("#")) continue;
                String[] f = line.split("\\|", -1);
                if (f.length < 6) continue;
                ps.setString(1, f[1]);
                if (f[0].matches("\\d+")) ps.setInt(2, Integer.parseInt(f[0])); else ps.setNull(2, java.sql.Types.INTEGER);
                ps.setString(3, f[4]);
                ps.setString(4, f[5]);
                ps.setString(5, "builtin:munou2014");
                ps.setDouble(6, now);
                ps.setString(7, f[2]);
                if (f[3].matches("\\d+")) ps.setLong(8, Long.parseLong(f[3])); else ps.setNull(8, java.sql.Types.INTEGER);
                n += ps.executeUpdate();
            }
            conn.commit();
        } catch (Exception e) {
            conn.rollback();
            throw e;
        } finally {
            conn.setAutoCommit(auto);
        }
        return n;
    }

    public List<String> maps() throws SQLException {
        List<String> out = new ArrayList<>();
        if (!hasTable("maps")) return out;
        try (Statement st = conn.createStatement();
             ResultSet rs = st.executeQuery("SELECT name FROM maps ORDER BY last_seen DESC")) {
            while (rs.next()) out.add(rs.getString(1));
        }
        return out;
    }

    /** マスごとの訪問回数 [x][y] (0 = 未訪問)。 */
    public int[][] cells(String map) throws SQLException {
        int[][] v = new int[SIZE][SIZE];
        if (map == null || !hasTable("cells")) return v;
        try (PreparedStatement ps = conn.prepareStatement("SELECT x, y, visits FROM cells WHERE map=?")) {
            ps.setString(1, map);
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    int x = rs.getInt(1), y = rs.getInt(2);
                    if (x >= 0 && x < SIZE && y >= 0 && y < SIZE) v[x][y] = rs.getInt(3);
                }
            }
        }
        return v;
    }

    public Pos latest() throws SQLException {
        if (!hasTable("positions")) return null;
        try (Statement st = conn.createStatement();
             ResultSet rs = st.executeQuery("SELECT map, x, y, ts FROM positions ORDER BY ts DESC LIMIT 1")) {
            return rs.next() ? new Pos(rs.getString(1), rs.getInt(2), rs.getInt(3), rs.getDouble(4)) : null;
        }
    }

    /** 最近の足跡 (古い順)。 */
    public List<Pos> trail(String map, int limit) throws SQLException {
        List<Pos> out = new ArrayList<>();
        if (map == null || !hasTable("positions")) return out;
        try (PreparedStatement ps = conn.prepareStatement(
                "SELECT map, x, y, ts FROM (SELECT * FROM positions WHERE map=? ORDER BY ts DESC LIMIT ?) ORDER BY ts")) {
            ps.setString(1, map);
            ps.setInt(2, limit);
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) out.add(new Pos(rs.getString(1), rs.getInt(2), rs.getInt(3), rs.getDouble(4)));
            }
        }
        return out;
    }

    public List<Spot> spots(String map) throws SQLException {
        List<Spot> out = new ArrayList<>();
        if (!hasTable("spots")) return out;
        String sql = "SELECT id, name, map, x, y, radius, kind, note FROM spots" + (map != null ? " WHERE map=?" : "") + " ORDER BY id";
        try (PreparedStatement ps = conn.prepareStatement(sql)) {
            if (map != null) ps.setString(1, map);
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next())
                    out.add(new Spot(rs.getLong(1), rs.getString(2), rs.getString(3), rs.getInt(4), rs.getInt(5),
                            rs.getInt(6), rs.getString(7), rs.getString(8)));
            }
        }
        return out;
    }

    public List<Event> events(int limit) throws SQLException {
        List<Event> out = new ArrayList<>();
        if (!hasTable("events")) return out;
        try (PreparedStatement ps = conn.prepareStatement(
                "SELECT ts, kind, map, x, y, detail FROM events ORDER BY ts DESC LIMIT ?")) {
            ps.setInt(1, limit);
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next())
                    out.add(new Event(rs.getDouble(1), rs.getString(2), rs.getString(3),
                            (Integer) rs.getObject(4), (Integer) rs.getObject(5), rs.getString(6)));
            }
        }
        return out;
    }

    /** そのマップのモンスター (レベル順)。map は英語名 (Atlans) でも取り込んだ時の名前 (アトランス1) でもよい。 */
    public List<Monster> monsters(String map) throws SQLException {
        List<Monster> out = new ArrayList<>();
        if (!hasTable("monsters")) return out;
        boolean hasIcon = hasColumn("monsters", "icon");
        boolean hasNote = hasColumn("monsters", "note");
        String sql = "SELECT name, MIN(level), map, " + (hasIcon ? "MAX(icon)" : "NULL") + ", "
                + (hasNote ? "MAX(note)" : "NULL") + " FROM monsters"
                + (map != null ? " WHERE map_en=? OR map=?" : "")
                + " GROUP BY name" + (map != null ? "" : ", map") + " ORDER BY MIN(level) IS NULL, MIN(level), name";
        try (PreparedStatement ps = conn.prepareStatement(sql)) {
            if (map != null) {
                ps.setString(1, map);
                ps.setString(2, map);
            }
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) out.add(new Monster(rs.getString(1), (Integer) rs.getObject(2), rs.getString(3), iconPath(rs.getString(4)), rs.getString(5)));
            }
        }
        return out;
    }

    /** 登録されているマップ名 (日本語名・英語名の両方) 一覧。 */
    public List<String> monsterMaps() throws SQLException {
        List<String> out = new ArrayList<>();
        if (!hasTable("monsters")) return out;
        try (Statement st = conn.createStatement();
             ResultSet rs = st.executeQuery("SELECT DISTINCT map_en FROM monsters WHERE map_en != '' UNION "
                     + "SELECT DISTINCT map FROM monsters WHERE map != '' ORDER BY 1")) {
            while (rs.next()) out.add(rs.getString(1));
        }
        return out;
    }

    /** マップ名 (日本語でも英語でも) をゲーム内の英語名に。分からなければそのまま。 */
    public String mapEn(String map) throws SQLException {
        if (hasTable("monsters")) {
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT map_en FROM monsters WHERE (map=? OR map_en=?) AND map_en != '' LIMIT 1")) {
                ps.setString(1, map);
                ps.setString(2, map);
                try (ResultSet rs = ps.executeQuery()) {
                    if (rs.next()) return rs.getString(1);
                }
            }
        }
        return map;
    }

    /** 手で登録するモンスター。stats = {生命, 最小攻撃力, 最大攻撃力, 防御力, 防御成功率, 攻撃成功率} (null 可)。 */
    public void saveMonster(String name, Integer level, List<String> maps, String icon, String note, Integer[] stats)
            throws Exception {
        ensureBuiltinMonsters(); // 表と列を用意
        try (PreparedStatement ps = conn.prepareStatement(
                "INSERT INTO monsters(name, level, map, map_en, source, updated, icon, note, hp, atk_min, atk_max, def, def_rate, atk_rate) "
                        + "VALUES (?,?,?,?,'manual',?,?,?,?,?,?,?,?,?) ON CONFLICT(name, map) DO UPDATE SET level=excluded.level, "
                        + "map_en=excluded.map_en, source='manual', updated=excluded.updated, "
                        + "icon=COALESCE(excluded.icon, monsters.icon), note=excluded.note, hp=excluded.hp, atk_min=excluded.atk_min, "
                        + "atk_max=excluded.atk_max, def=excluded.def, def_rate=excluded.def_rate, atk_rate=excluded.atk_rate")) {
            for (String map : maps.isEmpty() ? List.of("") : maps) {
                ps.setString(1, name);
                setInt(ps, 2, level);
                ps.setString(3, map);
                ps.setString(4, map.isEmpty() ? "" : mapEn(map));
                ps.setDouble(5, System.currentTimeMillis() / 1000.0);
                ps.setString(6, icon);
                ps.setString(7, note);
                for (int i = 0; i < 6; i++) setInt(ps, 8 + i, stats != null && i < stats.length ? stats[i] : null);
                ps.executeUpdate();
            }
        }
    }

    private static void setInt(PreparedStatement ps, int idx, Integer v) throws SQLException {
        if (v == null) ps.setNull(idx, java.sql.Types.INTEGER); else ps.setInt(idx, v);
    }

    /** アイコン画像の保存先 (DB と同じフォルダの monster_icons/)。 */
    public java.io.File iconDir() {
        return new java.io.File(new java.io.File(path).getAbsoluteFile().getParentFile(), "monster_icons");
    }

    /** 地点ごとの狙うモンスター。 */
    public java.util.Map<Long, List<String>> spotMonsters() throws SQLException {
        java.util.Map<Long, List<String>> out = new java.util.HashMap<>();
        if (!hasTable("spot_monsters")) return out;
        try (Statement st = conn.createStatement();
             ResultSet rs = st.executeQuery("SELECT spot_id, monster FROM spot_monsters ORDER BY rowid")) {
            while (rs.next()) out.computeIfAbsent(rs.getLong(1), k -> new ArrayList<>()).add(rs.getString(2));
        }
        return out;
    }

    public void setSpotMonsters(long spotId, List<String> names) throws SQLException {
        try (Statement st = conn.createStatement()) {
            st.execute("CREATE TABLE IF NOT EXISTS spot_monsters (spot_id INTEGER NOT NULL, monster TEXT NOT NULL, "
                    + "PRIMARY KEY (spot_id, monster))");
        }
        try (PreparedStatement del = conn.prepareStatement("DELETE FROM spot_monsters WHERE spot_id=?")) {
            del.setLong(1, spotId);
            del.executeUpdate();
        }
        try (PreparedStatement ins = conn.prepareStatement("INSERT OR IGNORE INTO spot_monsters(spot_id, monster) VALUES (?,?)")) {
            for (String n : names) {
                ins.setLong(1, spotId);
                ins.setString(2, n);
                ins.executeUpdate();
            }
        }
    }

    /** 登録して ID を返す。 */
    public long addSpot(String name, String map, int x, int y, int radius, String kind, String note) throws SQLException {
        ensureSpots();
        try (PreparedStatement ps = conn.prepareStatement(
                "INSERT INTO spots(name, map, x, y, radius, kind, note, created) VALUES (?,?,?,?,?,?,?,?)")) {
            ps.setString(1, name);
            ps.setString(2, map);
            ps.setInt(3, x);
            ps.setInt(4, y);
            ps.setInt(5, radius);
            ps.setString(6, kind);
            ps.setString(7, note);
            ps.setDouble(8, System.currentTimeMillis() / 1000.0);
            ps.executeUpdate();
        }
        try (Statement st = conn.createStatement(); ResultSet rs = st.executeQuery("SELECT last_insert_rowid()")) {
            return rs.next() ? rs.getLong(1) : -1;
        }
    }

    public void updateSpot(long id, String name, String map, int x, int y, int radius, String kind, String note) throws SQLException {
        try (PreparedStatement ps = conn.prepareStatement(
                "UPDATE spots SET name=?, map=?, x=?, y=?, radius=?, kind=?, note=? WHERE id=?")) {
            ps.setString(1, name);
            ps.setString(2, map);
            ps.setInt(3, x);
            ps.setInt(4, y);
            ps.setInt(5, radius);
            ps.setString(6, kind);
            ps.setString(7, note);
            ps.setLong(8, id);
            ps.executeUpdate();
        }
    }

    /** 地点の表を用意する (map_logger.py をまだ動かしていない DB でも登録できるように)。 */
    public void ensureSpots() throws SQLException {
        try (Statement st = conn.createStatement()) {
            st.execute("CREATE TABLE IF NOT EXISTS spots (id INTEGER PRIMARY KEY, name TEXT NOT NULL, map TEXT NOT NULL, "
                    + "x INTEGER NOT NULL, y INTEGER NOT NULL, radius INTEGER NOT NULL DEFAULT 5, "
                    + "kind TEXT NOT NULL DEFAULT 'hunt', note TEXT, created REAL)");
        }
    }

    public void deleteSpot(long id) throws SQLException {
        try (PreparedStatement ps = conn.prepareStatement("DELETE FROM spots WHERE id=?")) {
            ps.setLong(1, id);
            ps.executeUpdate();
        }
        if (hasTable("spot_monsters")) setSpotMonsters(id, List.of());
    }

    /** DB からの相対パスを絶対パスに (アイコンは DB と同じフォルダの monster_icons/ に保存される)。 */
    private String iconPath(String rel) {
        if (rel == null || rel.isEmpty()) return null;
        java.io.File f = new java.io.File(rel);
        if (!f.isAbsolute()) f = new java.io.File(new java.io.File(path).getAbsoluteFile().getParentFile(), rel);
        return f.exists() ? f.getPath() : null;
    }

    private boolean hasColumn(String table, String col) throws SQLException {
        try (Statement st = conn.createStatement(); ResultSet rs = st.executeQuery("PRAGMA table_info(" + table + ")")) {
            while (rs.next()) if (col.equalsIgnoreCase(rs.getString("name"))) return true;
        }
        return false;
    }

    private boolean hasTable(String name) throws SQLException {
        try (PreparedStatement ps = conn.prepareStatement("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?")) {
            ps.setString(1, name);
            try (ResultSet rs = ps.executeQuery()) {
                return rs.next();
            }
        }
    }

    @Override
    public void close() throws SQLException {
        conn.close();
    }
}
