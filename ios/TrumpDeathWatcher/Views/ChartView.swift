/*  The price charts — one renderer, two series, as makeChart() is on the web.
 *
 *  Hatched fill rather than a gradient: it reads as engraving, not dashboard.
 *  Green when the window closed up, red when it closed down, decided by first
 *  versus last close — the same test the web client makes.
 */

import SwiftUI

struct SeriesChart: View {
    let series: [SeriesPoint]
    /// Tooltip time label — dates for the market series, date + time for the
    /// coin, matching the two `labelFor` closures on the web.
    let label: (Double) -> String

    @State private var touchX: CGFloat?

    private var up: Bool {
        guard let first = series.first, let last = series.last else { return true }
        return last.c >= first.c
    }

    private var stroke: Color { up ? Ink.up : Ink.red }

    var body: some View {
        GeometryReader { geo in
            let size = geo.size
            ZStack(alignment: .topLeading) {
                Canvas { context, canvasSize in
                    draw(context: &context, size: canvasSize)
                }
                if let index = touchedIndex(width: size.width), series.count > 1 {
                    tooltip(for: index, in: size)
                }
            }
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { touchX = $0.location.x }
                    .onEnded { _ in touchX = nil }
            )
        }
        .frame(height: 190)                 // #chart { height: 190px }
    }

    // MARK: - geometry

    private let pad: CGFloat = 8

    private func bounds() -> (min: Double, span: Double) {
        let values = series.map(\.c)
        let lo = values.min() ?? 0
        let hi = values.max() ?? 0
        // `|| Math.abs(max) || 1` on the web: a flat series still needs a
        // non-zero span or every point divides by zero.
        let span = (hi - lo) != 0 ? (hi - lo) : (abs(hi) != 0 ? abs(hi) : 1)
        return (lo, span)
    }

    private func x(_ i: Int, width: CGFloat) -> CGFloat {
        let denominator = max(series.count - 1, 1)
        return CGFloat(Double(i) / Double(denominator)) * width
    }

    private func y(_ value: Double, height: CGFloat) -> CGFloat {
        let (lo, span) = bounds()
        return height - pad - CGFloat((value - lo) / span) * (height - pad * 2)
    }

    private func touchedIndex(width: CGFloat) -> Int? {
        guard let touchX, series.count > 1, width > 0 else { return nil }
        let raw = (touchX / width) * CGFloat(series.count - 1)
        return min(max(Int(raw.rounded()), 0), series.count - 1)
    }

    // MARK: - drawing

    private func draw(context: inout GraphicsContext, size: CGSize) {
        guard !series.isEmpty else { return }

        var baseline = Path()
        baseline.move(to: CGPoint(x: 0, y: size.height - pad))
        baseline.addLine(to: CGPoint(x: size.width, y: size.height - pad))
        context.stroke(baseline, with: .color(Ink.ink.opacity(0.35)), lineWidth: 1)

        // A token minutes old can have exactly one candle. A one-point path
        // draws nothing at all, so mark the level instead of rendering an
        // empty box.
        if series.count == 1 {
            let mid = size.height / 2
            var line = Path()
            line.move(to: CGPoint(x: 0, y: mid))
            line.addLine(to: CGPoint(x: size.width, y: mid))
            context.stroke(line, with: .color(stroke.opacity(0.65)),
                           style: StrokeStyle(lineWidth: 2, dash: [6, 5]))
            let r: CGFloat = 5
            context.fill(Path(ellipseIn: CGRect(x: size.width / 2 - r, y: mid - r,
                                                width: r * 2, height: r * 2)),
                         with: .color(stroke))
            return
        }

        var line = Path()
        for (i, point) in series.enumerated() {
            let p = CGPoint(x: x(i, width: size.width), y: y(point.c, height: size.height))
            if i == 0 { line.move(to: p) } else { line.addLine(to: p) }
        }

        var area = line
        area.addLine(to: CGPoint(x: size.width, y: size.height - pad))
        area.addLine(to: CGPoint(x: 0, y: size.height - pad))
        area.closeSubpath()

        // 45° hatching, clipped to the area under the line.
        context.drawLayer { layer in
            layer.clip(to: area)
            var hatch = Path()
            let spacing: CGFloat = 6 * 1.41421356        // perpendicular gap of 6
            var start = -size.height
            while start < size.width + size.height {
                hatch.move(to: CGPoint(x: start, y: size.height))
                hatch.addLine(to: CGPoint(x: start + size.height, y: 0))
                start += spacing
            }
            layer.stroke(hatch, with: .color(stroke.opacity(0.22)), lineWidth: 1.6)
        }

        context.stroke(line, with: .color(stroke),
                       style: StrokeStyle(lineWidth: 2.4, lineJoin: .round))

        if let index = touchedIndex(width: size.width) {
            let p = CGPoint(x: x(index, width: size.width),
                            y: y(series[index].c, height: size.height))
            context.fill(Path(ellipseIn: CGRect(x: p.x - 4, y: p.y - 4,
                                                width: 8, height: 8)),
                         with: .color(stroke))
        }
    }

    // MARK: - .chart-tip

    private func tooltip(for index: Int, in size: CGSize) -> some View {
        let point = series[index]
        let px = x(index, width: size.width)
        let py = y(point.c, height: size.height)
        return Text("\(Fmt.price(point.c)) — \(label(point.t))")
            .font(.mono(10.5))
            .foregroundStyle(Ink.paper)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(Ink.ink)
            .fixedSize()
            .offset(x: min(max(px - 70, 0), max(size.width - 150, 0)),
                    y: max(0, py - 36))
            .allowsHitTesting(false)
    }
}

/// The whole section: quote row, window chips, chart, source note. Shared by
/// the market and the coin, which differ only in what fills it.
struct QuoteSection<Chips: View, Footer: View>: View {
    let symbol: String
    let last: String
    let change: Double?
    let series: [SeriesPoint]
    let sourceNote: String
    let timeLabel: (Double) -> String
    @ViewBuilder let chips: Chips
    @ViewBuilder let footer: Footer

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // .quote-row
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text(symbol.uppercased())
                    .font(.condensed(30))
                    .foregroundStyle(Ink.ink)
                Text(last)
                    .font(.mono(24))
                    .foregroundStyle(Ink.ink)
                if let change {
                    Text(Fmt.pct(change))
                        .font(.mono(14, weight: .bold))
                        .foregroundStyle(change >= 0 ? Ink.up : Ink.red)
                }
                Spacer(minLength: 4)
                chips
            }
            .lineLimit(1)
            .minimumScaleFactor(0.7)
            .padding(.bottom, 8)
            Ink.ink.frame(height: 2)
                .padding(.bottom, 10)

            SeriesChart(series: series, label: timeLabel)

            // .src-note
            Text(sourceNote.uppercased())
                .font(.mono(10))
                .tracking(1.2)
                .foregroundStyle(Ink.inkFaint)
                .frame(maxWidth: .infinity, alignment: .trailing)
                .padding(.top, 4)

            footer
        }
    }
}

/// .chips — a hard-edged segmented control.
struct ChipGroup: View {
    let options: [(label: String, value: String)]
    let selection: String
    let onSelect: (String) -> Void

    var body: some View {
        HStack(spacing: 0) {
            ForEach(Array(options.enumerated()), id: \.offset) { index, option in
                if index > 0 { Ink.ink.frame(width: 1) }
                Button {
                    onSelect(option.value)
                } label: {
                    Text(option.label)
                        .font(.mono(10))
                        .tracking(1.2)
                        .foregroundStyle(selection == option.value ? Ink.paper : Ink.inkSoft)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(selection == option.value ? Ink.ink : Ink.paper)
                }
                .buttonStyle(.plain)
            }
        }
        .fixedSize()
        .overlay(Rectangle().strokeBorder(Ink.ink, lineWidth: 1))
    }
}
