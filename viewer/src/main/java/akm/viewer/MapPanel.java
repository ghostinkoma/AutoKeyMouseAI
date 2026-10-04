package akm.viewer;

import java.awt.BasicStroke;
import java.awt.Color;
import java.awt.Dimension;
import java.awt.Font;
import java.awt.Graphics;
import java.awt.Graphics2D;
import java.awt.Point;
import java.awt.RenderingHints;
import java.awt.event.MouseAdapter;
import java.awt.event.MouseEvent;
import java.awt.event.MouseWheelEvent;
import java.util.List;
import java.util.function.BiConsumer;
import javax.swing.JPanel;

/** 1 マップ (256x256 マス) を描く。ホイールで拡大縮小、ドラッグで移動。 */
public final class MapPanel extends JPanel {
    private int[][] cells = new int[MapDb.SIZE][MapDb.SIZE];
    private List<MapDb.Pos> trail = List.of();
    private List<MapDb.Spot> spots = List.of();
    private MapDb.Pos current;
    private double cell = 3.0;     // 1 マスのピクセル数
    private double offX = 10, offY = 10;
    private Point dragFrom;
    private int hoverX = -1, hoverY = -1;
    private BiConsumer<Integer, Integer> onHover = (x, y) -> {};
    private ContextHandler onContext = (x, y, e) -> {};

    public interface ContextHandler { void open(int x, int y, MouseEvent e); }

    public MapPanel() {
        setBackground(new Color(18, 18, 22));
        setPreferredSize(new Dimension(800, 800));
        MouseAdapter m = new MouseAdapter() {
            @Override public void mousePressed(MouseEvent e) {
                if (e.isPopupTrigger()) { popup(e); return; }
                dragFrom = e.getPoint();
            }
            @Override public void mouseReleased(MouseEvent e) {
                if (e.isPopupTrigger()) popup(e);
                dragFrom = null;
            }
            @Override public void mouseDragged(MouseEvent e) {
                if (dragFrom == null) return;
                offX += e.getX() - dragFrom.x;
                offY += e.getY() - dragFrom.y;
                dragFrom = e.getPoint();
                repaint();
            }
            @Override public void mouseMoved(MouseEvent e) {
                int[] c = toCell(e.getX(), e.getY());
                hoverX = c[0];
                hoverY = c[1];
                onHover.accept(hoverX, hoverY);
                repaint();
            }
            @Override public void mouseWheelMoved(MouseWheelEvent e) {
                double old = cell;
                cell = Math.max(1.0, Math.min(40.0, cell * (e.getWheelRotation() < 0 ? 1.25 : 0.8)));
                // マウスの位置を中心に拡大縮小
                offX = e.getX() - (e.getX() - offX) * cell / old;
                offY = e.getY() - (e.getY() - offY) * cell / old;
                repaint();
            }
            private void popup(MouseEvent e) {
                int[] c = toCell(e.getX(), e.getY());
                if (c[0] >= 0) onContext.open(c[0], c[1], e);
            }
        };
        addMouseListener(m);
        addMouseMotionListener(m);
        addMouseWheelListener(m);
    }

    public void onHover(BiConsumer<Integer, Integer> h) { onHover = h; }
    public void onContext(ContextHandler h) { onContext = h; }

    public void setData(int[][] cells, List<MapDb.Pos> trail, List<MapDb.Spot> spots, MapDb.Pos current) {
        this.cells = cells;
        this.trail = trail;
        this.spots = spots;
        this.current = current;
        repaint();
    }

    public int visits(int x, int y) {
        return x >= 0 && y >= 0 && x < MapDb.SIZE && y < MapDb.SIZE ? cells[x][y] : 0;
    }

    /** 画面全体にマップが収まるようにする。 */
    public void fit() {
        int w = Math.max(100, getWidth()), h = Math.max(100, getHeight());
        cell = Math.max(1.0, Math.min(w, h) / (double) (MapDb.SIZE + 4));
        offX = (w - cell * MapDb.SIZE) / 2;
        offY = (h - cell * MapDb.SIZE) / 2;
        repaint();
    }

    /** 指定マスが画面の中央に来るようにする。 */
    public void centerOn(int x, int y) {
        offX = getWidth() / 2.0 - (x + 0.5) * cell;
        offY = getHeight() / 2.0 - (y + 0.5) * cell;
        repaint();
    }

    private int[] toCell(int px, int py) {
        int x = (int) Math.floor((px - offX) / cell), y = (int) Math.floor((py - offY) / cell);
        if (x < 0 || y < 0 || x >= MapDb.SIZE || y >= MapDb.SIZE) return new int[] {-1, -1};
        return new int[] {x, y};
    }

    private static Color heat(int visits, int max) {
        // 訪問回数が多いほど明るい緑
        double t = Math.log1p(visits) / Math.log1p(Math.max(1, max));
        int g = (int) (90 + 165 * t);
        return new Color(30, g, 60);
    }

    @Override
    protected void paintComponent(Graphics g0) {
        super.paintComponent(g0);
        Graphics2D g = (Graphics2D) g0;
        g.setRenderingHint(RenderingHints.KEY_ANTIALIASING, RenderingHints.VALUE_ANTIALIAS_ON);
        int n = MapDb.SIZE;
        // 外枠と未訪問の地面
        g.setColor(new Color(40, 40, 48));
        g.fillRect((int) offX, (int) offY, (int) Math.ceil(cell * n), (int) Math.ceil(cell * n));
        int max = 1;
        for (int[] col : cells) for (int v : col) max = Math.max(max, v);
        int cs = Math.max(1, (int) Math.ceil(cell));
        for (int x = 0; x < n; x++) {
            for (int y = 0; y < n; y++) {
                int v = cells[x][y];
                if (v <= 0) continue;
                g.setColor(heat(v, max));
                g.fillRect((int) (offX + x * cell), (int) (offY + y * cell), cs, cs);
            }
        }
        // 細かい格子 (拡大時のみ)
        if (cell >= 8) {
            g.setColor(new Color(255, 255, 255, 25));
            for (int i = 0; i <= n; i++) {
                int p = (int) (offX + i * cell);
                g.drawLine(p, (int) offY, p, (int) (offY + n * cell));
                p = (int) (offY + i * cell);
                g.drawLine((int) offX, p, (int) (offX + n * cell), p);
            }
        }
        // 足跡
        if (trail.size() > 1) {
            g.setColor(new Color(255, 220, 0, 150));
            g.setStroke(new BasicStroke(Math.max(1f, (float) cell / 3)));
            for (int i = 1; i < trail.size(); i++) {
                MapDb.Pos a = trail.get(i - 1), b = trail.get(i);
                if (Math.abs(a.x() - b.x()) > 6 || Math.abs(a.y() - b.y()) > 6) continue; // ワープは結ばない
                g.drawLine(cx(a.x()), cy(a.y()), cx(b.x()), cy(b.y()));
            }
        }
        // 登録地点 (狩場など)
        g.setStroke(new BasicStroke(2f));
        g.setFont(getFont().deriveFont(Font.BOLD, 12f));
        for (MapDb.Spot s : spots) {
            Color c = switch (s.kind() == null ? "" : s.kind()) {
                case "hunt" -> new Color(255, 140, 0);
                case "shop" -> new Color(0, 200, 255);
                case "town", "safe" -> new Color(120, 160, 255);
                default -> new Color(255, 80, 200);
            };
            int r = (int) Math.max(4, s.radius() * cell);
            g.setColor(c);
            g.drawOval(cx(s.x()) - r, cy(s.y()) - r, 2 * r, 2 * r);
            g.fillOval(cx(s.x()) - 3, cy(s.y()) - 3, 6, 6);
            g.drawString(s.name(), cx(s.x()) + 6, cy(s.y()) - 6);
        }
        // 現在地
        if (current != null) {
            int r = (int) Math.max(5, cell * 1.5);
            g.setColor(Color.RED);
            g.fillOval(cx(current.x()) - r / 2, cy(current.y()) - r / 2, r, r);
            g.setColor(Color.WHITE);
            g.drawOval(cx(current.x()) - r, cy(current.y()) - r, 2 * r, 2 * r);
        }
        // マウス位置
        if (hoverX >= 0) {
            g.setColor(new Color(255, 255, 255, 120));
            g.drawRect((int) (offX + hoverX * cell), (int) (offY + hoverY * cell), cs, cs);
        }
    }

    private int cx(int x) { return (int) (offX + (x + 0.5) * cell); }
    private int cy(int y) { return (int) (offY + (y + 0.5) * cell); }
}
