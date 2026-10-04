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
    public record Event(double ts, String kind, String map, Integer x, Integer y, String detail) {}

    private final Connection conn;
    public final String path;

    public MapDb(String path) throws SQLException {
        this.path = path;
        conn = DriverManager.getConnection("jdbc:sqlite:" + path);
        try (Statement st = conn.createStatement()) {
            st.execute("PRAGMA busy_timeout=3000"); // Python が書き込み中なら少し待つ
        }
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

    public void addSpot(String name, String map, int x, int y, int radius, String kind, String note) throws SQLException {
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
