import StoreKit
import SwiftUI

struct SubjectScene: View {
    let subject: SubjectArea

    @State private var selectedChapter: Chapter?
    @Environment(\.requestReview) private var requestReview

    var body: some View {
        NavigationStack {
            SubjectBrowserView(
                subject: subject,
                selectedChapter: $selectedChapter
            )
        }
        .tint(subject.accent)
        .fullScreenCover(item: $selectedChapter) { chapter in
            ChapterReadingScene(subject: subject, initialChapter: chapter, onClose: askForRating)
        }
    }

    /// After a good reading session, request the App Store rating prompt when
    /// `ReviewPrompt` allows it, once the reader has finished closing and the
    /// chapter list is showing.
    private func askForRating(afterGoodSession goodSession: Bool) {
        guard goodSession, ReviewPrompt.shouldAutoRequest() else { return }
        ReviewPrompt.recordRequest()
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(700))
            requestReview()
        }
    }
}

/// A single navigation hierarchy survives opening or closing a foldable display.
/// Compact displays start in the reader; wide displays also expose the index.
private struct ChapterReadingScene: View {
    let subject: SubjectArea
    let onClose: (Bool) -> Void

    @State private var selection: Chapter?
    @State private var goodSession = false
    @State private var searchText = ""
    @State private var compactColumn = NavigationSplitViewColumn.detail
    @Environment(\.dismiss) private var dismiss

    init(subject: SubjectArea, initialChapter: Chapter, onClose: @escaping (Bool) -> Void) {
        self.subject = subject
        self.onClose = onClose
        // A one-time seed; the scene owns chapter selection after it opens.
        _selection = State(initialValue: initialChapter)
    }

    var body: some View {
        NavigationSplitView(preferredCompactColumn: $compactColumn) {
            List(selection: $selection) {
                ForEach(subject.chapters(matching: searchText)) { chapter in
                    NavigationLink(value: chapter) {
                        VStack(alignment: .leading, spacing: 5) {
                            Text("\(chapter.number). \(chapter.title)")
                                .font(.body.weight(.semibold))
                            Text("\(chapter.pageCount) pages")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        .padding(.vertical, 4)
                    }
                    .accessibilityIdentifier("reader.chapter.\(subject.slug).\(chapter.number)")
                }
            }
            .navigationTitle(subject.name)
            .searchable(text: $searchText, prompt: "Find a chapter")
            .navigationSplitViewColumnWidth(min: 260, ideal: 300, max: 380)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") { dismiss() }
                }
            }
        } detail: {
            NavigationStack {
                if let chapter = selection {
                    ChapterReaderView(
                        subject: subject,
                        chapter: chapter,
                        onClose: { goodSession = goodSession || $0 },
                        onDismiss: { dismiss() }
                    )
                    .id(chapter.id)
                } else {
                    ContentUnavailableView("Choose a chapter", systemImage: "book.closed")
                }
            }
        }
        .navigationSplitViewStyle(.balanced)
        .tint(subject.accent)
        .onDisappear { onClose(goodSession) }
    }
}

private struct SubjectBrowserView: View {
    let subject: SubjectArea
    @Binding var selectedChapter: Chapter?

    @State private var searchText = ""
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var filteredChapters: [Chapter] {
        subject.chapters(matching: searchText)
    }

    var body: some View {
        ZStack {
            NotebookBackground(accent: subject.accent)

            List {
                SubjectHero(subject: subject)
                    .listRowInsets(EdgeInsets(top: 14, leading: 0, bottom: 14, trailing: 16))
                    .listRowBackground(Color.clear)
                    .listRowSeparator(.hidden)

                Section {
                    if filteredChapters.isEmpty {
                        ContentUnavailableView.search(text: searchText)
                            .frame(maxWidth: .infinity, minHeight: 260)
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                    } else {
                        ForEach(filteredChapters) { chapter in
                            Button {
                                selectedChapter = chapter
                            } label: {
                                ChapterRow(
                                    chapter: chapter,
                                    subject: subject
                                )
                            }
                            .buttonStyle(NotebookRowButtonStyle(color: subject.accent))
                            .listRowInsets(
                                EdgeInsets(top: 0, leading: 0, bottom: 0, trailing: 12)
                            )
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                            .accessibilityHint("Opens the bundled handwritten PDF full screen")
                            .accessibilityIdentifier("chapter.\(subject.slug).\(chapter.number)")
                        }
                    }
                } header: {
                    DoodleSectionTitle(
                        title: "Chapter index",
                        color: subject.accent,
                        annotation: searchText.isEmpty
                            ? "\(subject.chapters.count) total"
                            : "\(filteredChapters.count) shown"
                    )
                    .textCase(nil)
                    .padding(.leading, 56)
                    .padding(.trailing, 8)
                    .padding(.top, 4)
                }
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .readableWidth()
        }
        .navigationTitle("PCM Notes")
        .navigationBarTitleDisplayMode(.inline)
        .searchable(text: $searchText, prompt: "Find a chapter")
        .animation(reduceMotion ? nil : .snappy(duration: 0.24), value: filteredChapters)
    }
}

private struct SubjectHero: View {
    let subject: SubjectArea

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    private var examLabel: String {
        subject == .mathematics ? "JEE MAIN + ADVANCED" : "JEE + NEET REVISION"
    }

    var body: some View {
        ZStack(alignment: .topTrailing) {
            VStack(alignment: .leading, spacing: 11) {
                EyebrowLabel(text: examLabel, color: subject.accent)

                Text(subject.name)
                    .font(.custom("Noteworthy-Bold", size: 42, relativeTo: .largeTitle))
                    .foregroundStyle(.primary)
                    .markerHighlight(subject.secondaryAccent)
                    .fixedSize(horizontal: false, vertical: true)

                Text(subject.tagline)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.trailing, dynamicTypeSize.isAccessibilitySize ? 0 : 78)

                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 24) { heroMetrics }
                    VStack(alignment: .leading, spacing: 12) { heroMetrics }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.leading, 72)

            if !dynamicTypeSize.isAccessibilitySize {
                SubjectDoodle(subject: subject)
                    .frame(width: 104, height: 104)
                    .offset(x: 8, y: 4)
            }
        }
        .padding(.vertical, 14)
    }

    @ViewBuilder
    private var heroMetrics: some View {
        MetricPill(
            value: "\(subject.chapters.count)",
            label: "chapters",
            color: subject.accent
        )
        MetricPill(
            value: subject.pageCount.formatted(),
            label: "pages",
            color: subject.secondaryAccent
        )
    }
}

private struct ChapterRow: View {
    let chapter: Chapter
    let subject: SubjectArea

    @AppStorage private var savedPage: Int
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    init(chapter: Chapter, subject: SubjectArea) {
        self.chapter = chapter
        self.subject = subject
        _savedPage = AppStorage(
            wrappedValue: 0,
            chapter.readingProgressKey(for: subject)
        )
    }

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            numberBadge

            chapterCopy
                .padding(.leading, 12)

            Spacer(minLength: 8)

            if !dynamicTypeSize.isAccessibilitySize {
                Image(systemName: "arrow.up.right")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(subject.accent.opacity(0.76))
                    .rotationEffect(.degrees(-8))
                    .padding(.top, 7)
            }
        }
        .padding(.vertical, 10)
        .contentShape(Rectangle())
        .overlay(alignment: .bottom) {
            HStack(spacing: 0) {
                Color.clear.frame(width: 56)
                DoodleRule(color: subject.accent)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Chapter \(chapter.number), \(chapter.title)")
        .accessibilityValue(progressLabel)
    }

    private var numberBadge: some View {
        ZStack {
            DoodleLoop(color: subject.accent)

            Text(String(format: "%02d", chapter.number))
                .font(.system(.caption, design: .monospaced, weight: .black))
                .foregroundStyle(subject.accent)
        }
        .frame(width: 56, height: 42)
        .rotationEffect(.degrees(chapter.number.isMultiple(of: 2) ? 1.4 : -1.2))
    }

    private var chapterCopy: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(chapter.title)
                .font(.system(.body, design: .rounded, weight: .bold))
                .foregroundStyle(.primary)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)

            Label(
                progressLabel,
                systemImage: savedPage > 1 ? "bookmark.fill" : "doc.text"
            )
                .font(.caption.weight(.medium))
                .foregroundStyle(.secondary)
                .symbolRenderingMode(.hierarchical)
                .fixedSize(horizontal: false, vertical: true)

            if savedPage > 1 {
                DoodleProgressLine(
                    value: Double(savedPage),
                    total: Double(chapter.pageCount),
                    color: subject.accent
                )
                .accessibilityLabel("Reading progress")
                .accessibilityValue("Page \(savedPage) of \(chapter.pageCount)")
            }
        }
    }

    private var progressLabel: String {
        chapter.progressSummary(savedPage: savedPage)
    }
}

private struct NotebookRowButtonStyle: ButtonStyle {
    let color: Color

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(configuration.isPressed ? color.opacity(0.08) : Color.clear)
    }
}

private struct SubjectDoodle: View {
    let subject: SubjectArea

    var body: some View {
        Canvas { context, size in
            let ink = subject.accent.opacity(0.58)
            let faintInk = subject.secondaryAccent.opacity(0.30)

            switch subject {
            case .physics:
                let center = CGPoint(x: size.width * 0.54, y: size.height * 0.50)
                for rotation in [-0.68, 0.05, 0.72] {
                    context.drawLayer { layer in
                        layer.translateBy(x: center.x, y: center.y)
                        layer.rotate(by: .radians(rotation))
                        layer.translateBy(x: -center.x, y: -center.y)
                        layer.stroke(
                            Path(ellipseIn: CGRect(
                                x: size.width * 0.16,
                                y: size.height * 0.34,
                                width: size.width * 0.76,
                                height: size.height * 0.32
                            )),
                            with: .color(ink),
                            style: StrokeStyle(lineWidth: 1.6, lineCap: .round)
                        )
                    }
                }
                context.fill(
                    Path(ellipseIn: CGRect(x: center.x - 5, y: center.y - 5, width: 10, height: 10)),
                    with: .color(subject.accent.opacity(0.82))
                )

            case .chemistry:
                var flask = Path()
                flask.move(to: CGPoint(x: size.width * 0.43, y: size.height * 0.14))
                flask.addLine(to: CGPoint(x: size.width * 0.57, y: size.height * 0.14))
                flask.addLine(to: CGPoint(x: size.width * 0.57, y: size.height * 0.42))
                flask.addLine(to: CGPoint(x: size.width * 0.82, y: size.height * 0.82))
                flask.addCurve(
                    to: CGPoint(x: size.width * 0.18, y: size.height * 0.82),
                    control1: CGPoint(x: size.width * 0.72, y: size.height * 0.94),
                    control2: CGPoint(x: size.width * 0.28, y: size.height * 0.94)
                )
                flask.addLine(to: CGPoint(x: size.width * 0.43, y: size.height * 0.42))
                flask.closeSubpath()
                context.stroke(
                    flask,
                    with: .color(ink),
                    style: StrokeStyle(lineWidth: 2, lineCap: .round, lineJoin: .round)
                )

                for bubble in [
                    CGRect(x: size.width * 0.30, y: size.height * 0.61, width: 10, height: 10),
                    CGRect(x: size.width * 0.52, y: size.height * 0.55, width: 7, height: 7),
                    CGRect(x: size.width * 0.62, y: size.height * 0.69, width: 12, height: 12),
                ] {
                    context.stroke(Path(ellipseIn: bubble), with: .color(faintInk), lineWidth: 1.6)
                }

            case .mathematics:
                var axes = Path()
                axes.move(to: CGPoint(x: size.width * 0.12, y: size.height * 0.76))
                axes.addLine(to: CGPoint(x: size.width * 0.91, y: size.height * 0.76))
                axes.move(to: CGPoint(x: size.width * 0.24, y: size.height * 0.91))
                axes.addLine(to: CGPoint(x: size.width * 0.24, y: size.height * 0.12))
                context.stroke(
                    axes,
                    with: .color(ink),
                    style: StrokeStyle(lineWidth: 1.5, lineCap: .round)
                )

                var curve = Path()
                curve.move(to: CGPoint(x: size.width * 0.18, y: size.height * 0.25))
                curve.addCurve(
                    to: CGPoint(x: size.width * 0.85, y: size.height * 0.24),
                    control1: CGPoint(x: size.width * 0.40, y: size.height * 0.98),
                    control2: CGPoint(x: size.width * 0.62, y: size.height * 0.98)
                )
                context.stroke(
                    curve,
                    with: .color(subject.secondaryAccent.opacity(0.68)),
                    style: StrokeStyle(lineWidth: 2.2, lineCap: .round)
                )
            }
        }
        .rotationEffect(.degrees(subject == .chemistry ? -3 : 2))
        .accessibilityHidden(true)
    }
}

#Preview("iPhone") {
    SubjectScene(subject: .physics)
}

#Preview("iPad", traits: .landscapeLeft) {
    SubjectScene(subject: .chemistry)
}
