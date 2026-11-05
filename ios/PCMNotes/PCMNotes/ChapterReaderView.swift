import PDFKit
import SwiftUI

private enum PDFReadingLayout: String, CaseIterable, Identifiable {
    case continuous
    case spread

    var id: String { rawValue }

    var title: String {
        switch self {
        case .continuous: "Continuous"
        case .spread: "Two-page spread"
        }
    }

    var symbol: String {
        switch self {
        case .continuous: "rectangle.portrait"
        case .spread: "rectangle.split.2x1"
        }
    }
}

struct ChapterReaderView: View {
    let subject: SubjectArea
    let chapter: Chapter
    /// Called as the reader closes, with whether this visit was a good reading
    /// session (see `ReviewPrompt`). Kept local until then, so reading never
    /// redraws the chapter list behind the reader.
    let onClose: (_ goodSession: Bool) -> Void
    let onDismiss: (() -> Void)?

    @State private var currentPage: Int
    @State private var renderedPageCount = 0
    @State private var pagesViewed: Set<Int> = []
    @State private var goodSession = false
    @Environment(\.dismiss) private var dismiss
    @Environment(\.colorScheme) private var colorScheme

    init(subject: SubjectArea, chapter: Chapter, onClose: @escaping (_ goodSession: Bool) -> Void = { _ in }, onDismiss: (() -> Void)? = nil) {
        self.subject = subject
        self.chapter = chapter
        self.onClose = onClose
        self.onDismiss = onDismiss

        let storedPage = UserDefaults.standard.integer(
            forKey: chapter.readingProgressKey(for: subject)
        )
        _currentPage = State(
            initialValue: chapter.resumePage(fromSaved: storedPage)
        )
    }

    private var pdfURL: URL? {
        chapter.pdfURL(for: subject)
    }

    var body: some View {
        Group {
            if let pdfURL {
                FocusPDFReader(
                    title: chapter.title,
                    pdfURL: pdfURL,
                    accent: subject.accent,
                    currentPage: $currentPage,
                    pageCount: $renderedPageCount,
                    onDismiss: onDismiss
                )
            } else {
                missingPDF
            }
        }
        .onChange(of: currentPage) { _, newPage in
            UserDefaults.standard.set(
                newPage,
                forKey: chapter.readingProgressKey(for: subject)
            )
        }
        .onChange(of: currentPage, initial: true) { _, newPage in
            pagesViewed.insert(newPage)
            // In a two-page spread the last page shows beside the one before it.
            let reachedEnd = renderedPageCount > 0 && newPage >= renderedPageCount - 1
            if !goodSession, ReviewPrompt.isGoodSession(pagesViewed: pagesViewed.count, reachedEnd: reachedEnd) {
                goodSession = true
            }
        }
        .onDisappear {
            onClose(goodSession)
        }
        .onChange(of: goodSession) { _, isGoodSession in
            if isGoodSession { onClose(true) }
        }
    }

    private var missingPDF: some View {
        ZStack {
            (colorScheme == .dark ? AppPalette.ink : AppPalette.paper)
                .ignoresSafeArea()

            ContentUnavailableView {
                Label("PDF unavailable", systemImage: "doc.badge.exclamationmark")
            } description: {
                Text("Rebuild and reinstall PCM Notes to embed this chapter's offline PDF.")
            } actions: {
                Button("Return to chapters") {
                    if let onDismiss { onDismiss() } else { dismiss() }
                }
                .buttonStyle(.borderedProminent)
                .tint(subject.accent)
            }
            .padding(30)
        }
    }
}

/// Page arithmetic behind the reader controls. In the iPad two-page spread the
/// first page stands alone like a book cover, then pages pair up: 2–3, 4–5, …
struct ReaderPagination: Equatable {
    var currentPage: Int
    var pageCount: Int
    var spreadMode: Bool

    /// First page of the spread that shows `currentPage`.
    var spreadStart: Int {
        guard currentPage > 1 else { return 1 }
        return currentPage.isMultiple(of: 2) ? currentPage : currentPage - 1
    }

    /// First page of the chapter's last spread.
    var lastSpreadStart: Int {
        guard pageCount > 1 else { return 1 }
        return pageCount.isMultiple(of: 2) ? pageCount : pageCount - 1
    }

    var canGoBack: Bool {
        currentPage > 1
    }

    var canGoForward: Bool {
        spreadMode ? spreadStart < lastSpreadStart : currentPage < pageCount
    }

    /// The page Previous moves to.
    var previousPage: Int {
        guard spreadMode else { return currentPage - 1 }
        return spreadStart <= 2 ? 1 : spreadStart - 2
    }

    /// The page Next moves to.
    var nextPage: Int {
        guard spreadMode else { return currentPage + 1 }
        return spreadStart == 1
            ? min(2, pageCount)
            : min(spreadStart + 2, pageCount)
    }

    /// The visible counter, e.g. "3 / 9" or "2–3 / 9" for a spread.
    var counterLabel: String {
        guard spreadMode, spreadStart > 1 else {
            return "\(currentPage) / \(pageCount)"
        }

        let spreadEnd = min(spreadStart + 1, pageCount)
        return "\(spreadStart)–\(spreadEnd) / \(pageCount)"
    }

    /// The spoken slider position, e.g. "Page 3 of 9".
    var positionLabel: String {
        guard spreadMode, spreadStart > 1 else {
            return "Page \(currentPage) of \(pageCount)"
        }

        let spreadEnd = min(spreadStart + 1, pageCount)
        return "Pages \(spreadStart) through \(spreadEnd) of \(pageCount)"
    }

    /// The page for a slider position, kept within the chapter.
    func page(forSliderValue value: Double) -> Int {
        min(max(Int(value.rounded()), 1), max(pageCount, 1))
    }
}

private struct ReaderControlBar: View {
    @Binding var currentPage: Int
    let pageCount: Int
    let accent: Color
    let spreadMode: Bool

    private var pagination: ReaderPagination {
        ReaderPagination(
            currentPage: currentPage,
            pageCount: pageCount,
            spreadMode: spreadMode
        )
    }

    private var sliderValue: Binding<Double> {
        Binding {
            Double(currentPage)
        } set: { newValue in
            currentPage = pagination.page(forSliderValue: newValue)
        }
    }

    var body: some View {
        HStack(spacing: 12) {
            pageButton(
                symbol: "chevron.left",
                label: "Previous page",
                isDisabled: !pagination.canGoBack
            ) {
                currentPage = pagination.previousPage
            }

            Slider(
                value: sliderValue,
                in: 1...Double(max(pageCount, 2)),
                step: 1
            )
            .tint(accent)
            .accessibilityLabel("Page position")
            .accessibilityValue(pagination.positionLabel)
            .accessibilityIdentifier("reader.pageSlider")

            Text(pagination.counterLabel)
                .font(.caption.monospacedDigit().weight(.bold))
                .foregroundStyle(.secondary)
                .frame(minWidth: spreadMode ? 78 : 54, alignment: .trailing)

            pageButton(
                symbol: "chevron.right",
                label: "Next page",
                isDisabled: !pagination.canGoForward
            ) {
                currentPage = pagination.nextPage
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .notebookGlass(tint: accent, cornerRadius: 19, interactive: true)
    }

    private func pageButton(
        symbol: String,
        label: String,
        isDisabled: Bool,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.caption.weight(.black))
                .frame(width: 32, height: 32)
                .background(accent.opacity(0.11), in: Circle())
        }
        .buttonStyle(.plain)
        .disabled(isDisabled)
        .accessibilityLabel(label)
    }
}

private struct FocusPDFReader: View {
    let title: String
    let pdfURL: URL
    let accent: Color
    @Binding var currentPage: Int
    @Binding var pageCount: Int
    let onDismiss: (() -> Void)?

    @Environment(\.dismiss) private var dismiss
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    @AppStorage("reader.ipad.layout") private var storedLayout = PDFReadingLayout.continuous.rawValue

    private var readingLayout: PDFReadingLayout {
        // Keep the preference while folded; a compact reader always uses one page.
        guard horizontalSizeClass == .regular else { return .continuous }
        return PDFReadingLayout(rawValue: storedLayout) ?? .continuous
    }

    private var readerBackdrop: Color {
        colorScheme == .dark ? .black : Color(uiColor: .systemGray5)
    }

    private var chromeBackground: Color {
        colorScheme == .dark ? .black.opacity(0.78) : AppPalette.paper.opacity(0.94)
    }

    private var chromeButtonBackground: Color {
        colorScheme == .dark ? .black.opacity(0.62) : .white.opacity(0.86)
    }

    var body: some View {
        ZStack {
            readerBackdrop.ignoresSafeArea()

            LocalPDFView(
                url: pdfURL,
                readingLayout: readingLayout,
                currentPage: $currentPage,
                pageCount: $pageCount
            )
            .background(readerBackdrop)
        }
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(chromeBackground, for: .navigationBar)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                Button {
                    if let onDismiss { onDismiss() } else { dismiss() }
                } label: {
                    Image(systemName: "xmark")
                        .font(.headline)
                        .frame(width: 40, height: 40)
                }
                .buttonStyle(.plain)
                .background(chromeButtonBackground, in: Circle())
                .accessibilityLabel("Close focus reader")
            }
            ToolbarItemGroup(placement: .primaryAction) {
                if horizontalSizeClass == .regular {
                    Menu {
                        Picker("Page layout", selection: $storedLayout) {
                            ForEach(PDFReadingLayout.allCases) { layout in
                                Label(layout.title, systemImage: layout.symbol)
                                    .tag(layout.rawValue)
                            }
                        }
                    } label: {
                        Image(systemName: readingLayout.symbol)
                            .font(.headline)
                            .frame(width: 40, height: 40)
                    }
                    .buttonStyle(.plain)
                    .background(chromeButtonBackground, in: Circle())
                    .accessibilityLabel("Reading layout")
                    .accessibilityValue(readingLayout.title)
                    .accessibilityIdentifier("reader.layoutMenu")
                }

                ShareLink(item: pdfURL) {
                    Image(systemName: "square.and.arrow.up")
                        .font(.headline)
                        .frame(width: 40, height: 40)
                }
                .buttonStyle(.plain)
                .background(chromeButtonBackground, in: Circle())
                .accessibilityLabel("Share chapter PDF")
            }
        }
        .safeAreaInset(edge: .bottom) {
            ReaderControlBar(
                currentPage: $currentPage,
                pageCount: max(pageCount, 1),
                accent: accent,
                spreadMode: readingLayout == .spread
            )
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .background(chromeBackground)
        }
    }
}

private struct LocalPDFView: UIViewRepresentable {
    let url: URL
    let readingLayout: PDFReadingLayout
    @Binding var currentPage: Int
    @Binding var pageCount: Int

    func makeCoordinator() -> Coordinator {
        Coordinator(parent: self)
    }

    func makeUIView(context: Context) -> PDFView {
        let pdfView = ResizablePDFView()
        pdfView.backgroundColor = .clear
        pdfView.displayMode = .singlePageContinuous
        pdfView.displayDirection = .vertical
        pdfView.displaysPageBreaks = true
        pdfView.pageBreakMargins = UIEdgeInsets(top: 10, left: 8, bottom: 10, right: 8)
        pdfView.displayBox = .cropBox
        pdfView.autoScales = true
        configure(pdfView, for: readingLayout)
        context.coordinator.observe(pdfView)
        loadDocument(into: pdfView, coordinator: context.coordinator)
        return pdfView
    }

    func updateUIView(_ pdfView: PDFView, context: Context) {
        context.coordinator.parent = self

        if context.coordinator.loadedURL != url {
            loadDocument(into: pdfView, coordinator: context.coordinator)
            return
        }

        if context.coordinator.loadedLayout != readingLayout {
            context.coordinator.loadedLayout = readingLayout
            configure(pdfView, for: readingLayout)
            refit(pdfView)
        }

        guard
            let document = pdfView.document,
            let visiblePage = pdfView.currentPage
        else { return }

        let targetIndex = min(max(currentPage - 1, 0), max(document.pageCount - 1, 0))
        if document.index(for: visiblePage) != targetIndex,
           let targetPage = document.page(at: targetIndex) {
            pdfView.go(to: targetPage)
        }
    }

    static func dismantleUIView(_ pdfView: PDFView, coordinator: Coordinator) {
        coordinator.stopObserving()
        pdfView.document = nil
    }

    private func loadDocument(into pdfView: PDFView, coordinator: Coordinator) {
        guard let document = PDFDocument(url: url) else { return }

        coordinator.loadedURL = url
        coordinator.loadedLayout = readingLayout
        pdfView.document = document
        pdfView.autoScales = true
        let documentPageCount = document.pageCount

        DispatchQueue.main.async {
            pageCount = documentPageCount
        }

        let targetIndex = min(max(currentPage - 1, 0), max(document.pageCount - 1, 0))

        refit(pdfView, targetIndex: targetIndex)
    }

    private func configure(_ pdfView: PDFView, for layout: PDFReadingLayout) {
        switch layout {
        case .continuous:
            pdfView.displayMode = .singlePageContinuous
            pdfView.displaysAsBook = false
        case .spread:
            pdfView.displayMode = .twoUpContinuous
            pdfView.displaysAsBook = true
        }
        pdfView.displayDirection = .vertical
        pdfView.autoScales = true
    }

    private func refit(_ pdfView: PDFView, targetIndex: Int? = nil) {
        let fallbackIndex = min(
            max(currentPage - 1, 0),
            max((pdfView.document?.pageCount ?? 1) - 1, 0)
        )

        DispatchQueue.main.async {
            let fitScale = pdfView.scaleFactorForSizeToFit
            if fitScale > 0 {
                pdfView.scaleFactor = fitScale
                pdfView.autoScales = true
            }

            if let targetPage = pdfView.document?.page(at: targetIndex ?? fallbackIndex) {
                pdfView.go(to: targetPage)
            }
        }
    }

    final class Coordinator: NSObject {
        var parent: LocalPDFView
        var loadedURL: URL?
        var loadedLayout: PDFReadingLayout?
        private var pageObserver: NSObjectProtocol?

        init(parent: LocalPDFView) {
            self.parent = parent
        }

        func observe(_ pdfView: PDFView) {
            pageObserver = NotificationCenter.default.addObserver(
                forName: .PDFViewPageChanged,
                object: pdfView,
                queue: .main
            ) { [weak self, weak pdfView] _ in
                guard
                    let self,
                    let pdfView,
                    let document = pdfView.document,
                    let page = pdfView.currentPage
                else { return }

                let visiblePage = document.index(for: page) + 1
                let documentPageCount = document.pageCount

                DispatchQueue.main.async { [weak self] in
                    guard let self else { return }

                    if parent.currentPage != visiblePage {
                        parent.currentPage = visiblePage
                    }
                    if parent.pageCount != documentPageCount {
                        parent.pageCount = documentPageCount
                    }
                }
            }
        }

        func stopObserving() {
            if let pageObserver {
                NotificationCenter.default.removeObserver(pageObserver)
            }
            pageObserver = nil
        }

        deinit {
            stopObserving()
        }
    }
}

/// PDFKit does not reliably refit a document when its container changes between
/// the Duo's displays or when a sidebar is toggled. Keep the visible page while
/// fitting the new proposal; normal scrolling and pinch zoom do not trigger this.
private final class ResizablePDFView: PDFView {
    private var lastSize = CGSize.zero

    override func layoutSubviews() {
        let page = currentPage
        super.layoutSubviews()
        guard bounds.size != lastSize, bounds.width > 0, bounds.height > 0 else { return }
        lastSize = bounds.size
        let fitScale = scaleFactorForSizeToFit
        if fitScale > 0 { scaleFactor = fitScale }
        if let page { go(to: page) }
    }
}

#Preview {
    ChapterReaderView(subject: .physics, chapter: ChapterCatalog.physics[0])
}
