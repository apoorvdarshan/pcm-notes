import SwiftUI

enum LegalDocument: String, Identifiable {
    case privacy
    case terms

    var id: String { rawValue }

    var title: String {
        switch self {
        case .privacy: "Privacy Policy"
        case .terms: "Terms of Use"
        }
    }

    var shortDescription: String {
        switch self {
        case .privacy: "What stays on your device"
        case .terms: "Rules for using PCM Notes"
        }
    }

    var symbol: String {
        switch self {
        case .privacy: "hand.raised"
        case .terms: "doc.text"
        }
    }

    var accent: Color {
        switch self {
        case .privacy: AppPalette.chemistry
        case .terms: AppPalette.mathematics
        }
    }

    fileprivate var eyebrow: String {
        switch self {
        case .privacy: "Your data, in plain language"
        case .terms: "The notebook rules"
        }
    }

    fileprivate var introduction: String {
        switch self {
        case .privacy:
            "PCM Notes is designed as an offline reader. It does not require an account and does not send your reading activity to us."
        case .terms:
            "These terms explain how you may use PCM Notes and its bundled study material. By using the app, you agree to them."
        }
    }

    var sections: [LegalSectionContent] {
        switch self {
        case .privacy:
            [
                LegalSectionContent(
                    title: "What the app stores",
                    body: "The chapter PDFs are bundled inside the app and open from your device. PCM Notes stores only reading progress and preferences on your device, including your last page, reader layout on iPad, and System, Light, or Dark appearance choice."
                ),
                LegalSectionContent(
                    title: "What we do not collect",
                    body: "The app has no account system, advertising, reading analytics, cross-app tracking, or profiling. We do not collect your reading history, sell personal data, or share app activity with data brokers. RevenueCat provides aggregate analytics only for optional App Store purchases."
                ),
                LegalSectionContent(
                    title: "Optional App Store tips",
                    body: "PCM Notes offers optional, repeatable consumable tips. Apple shows the local price and processes payment credentials. RevenueCat receives purchase history and an anonymous App User ID to validate receipts, complete the purchase flow, and provide aggregate purchase analytics. Neither PCM Notes nor RevenueCat receives payment-card details. Tips unlock nothing and are not charitable donations."
                ),
                LegalSectionContent(
                    title: "When you contact us",
                    body: "If you choose an email link, your email app and provider handle the message and send the details you include to us. If you choose the X link, the X app or website handles that visit under its own privacy policy. PCM Notes does not silently send either service information."
                ),
                LegalSectionContent(
                    title: "Control of local data",
                    body: "Reading progress and preferences remain in the app's local storage. Removing the app removes that locally stored app data, subject to your device backups and Apple's restore behaviour."
                ),
                LegalSectionContent(
                    title: "Questions about privacy",
                    body: "Email ad13dtu@gmail.com. You can also contact @apoorvdarshan on X or @apoorvxi on Instagram."
                )
            ]
        case .terms:
            [
                LegalSectionContent(
                    title: "Personal study use",
                    body: "PCM Notes and its bundled material are provided for your personal, non-commercial study and revision. Do not resell, redistribute, republish, scrape, or package the app or PDFs as another product."
                ),
                LegalSectionContent(
                    title: "Ownership and licence",
                    body: "The app, its design, and the bundled compilation remain protected by their applicable ownership and licence rights. Installing the app gives you a limited, revocable, non-transferable right to use them for personal study; it does not transfer ownership."
                ),
                LegalSectionContent(
                    title: "Optional tips",
                    body: "The Support tab may offer repeatable consumable tips through Apple's In-App Purchase system and RevenueCat. Tipping is optional, unlocks no content or feature, and is not a charitable donation. Apple displays and charges the price, RevenueCat validates and records the purchase, and Apple's terms govern billing and refunds. Finished consumable tips are not restorable purchases."
                ),
                LegalSectionContent(
                    title: "Independent resource",
                    body: "PCM Notes is independent and is not affiliated with, endorsed by, or an official product of any IIT, NTA, JEE, NEET, or other examination authority. Names and marks belong to their respective owners."
                ),
                LegalSectionContent(
                    title: "Educational disclaimer",
                    body: "The notes are revision material, not professional advice or a guarantee of accuracy, completeness, marks, rank, admission, or exam results. Check important material against your current syllabus and official sources."
                ),
                LegalSectionContent(
                    title: "Updates and availability",
                    body: "Chapters, features, compatibility, and availability may be corrected, changed, suspended, or discontinued. Updates may also revise these terms; the date on this page shows the current version."
                ),
                LegalSectionContent(
                    title: "External services",
                    body: "Apple's App Store processes optional tips and RevenueCat validates and records their purchase history. Email and X links leave PCM Notes and are operated by third parties. Their availability, content, privacy practices, and terms are outside our control."
                ),
                LegalSectionContent(
                    title: "Liability",
                    body: "To the fullest extent allowed by applicable law, PCM Notes is provided as available without warranties, and its creator is not liable for indirect, incidental, or consequential loss arising from use of the app or reliance on its material. Rights that cannot legally be excluded remain unaffected."
                ),
                LegalSectionContent(
                    title: "Contact",
                    body: "Questions about these terms can be sent to ad13dtu@gmail.com, or to @apoorvdarshan on X or @apoorvxi on Instagram."
                )
            ]
        }
    }
}

struct LegalSectionContent: Identifiable {
    let title: String
    let body: String

    var id: String { title }
}

struct LegalLinksSection: View {
    let color: Color
    var annotation = "read the fine print"

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            DoodleSectionTitle(
                title: "Legal",
                color: color,
                annotation: annotation
            )

            ForEach([LegalDocument.privacy, .terms]) { document in
                NavigationLink {
                    LegalDocumentView(document: document)
                } label: {
                    HStack(spacing: 14) {
                        Image(systemName: document.symbol)
                            .font(.title3.weight(.semibold))
                            .foregroundStyle(document.accent)
                            .frame(width: 30)

                        VStack(alignment: .leading, spacing: 3) {
                            Text(document.title)
                                .font(.headline)
                                .foregroundStyle(.primary)

                            Text(document.shortDescription)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }

                        Spacer(minLength: 8)

                        Image(systemName: "arrow.right")
                            .font(.callout.weight(.bold))
                            .foregroundStyle(document.accent)
                            .rotationEffect(.degrees(-4))
                    }
                    .padding(.vertical, 15)
                    .contentShape(Rectangle())
                    .overlay(alignment: .bottom) {
                        DoodleRule(color: document.accent)
                    }
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("legal.\(document.rawValue)")
                .accessibilityHint("Opens \(document.title)")
            }
        }
    }
}

struct LegalDocumentView: View {
    let document: LegalDocument

    var body: some View {
        ZStack {
            NotebookBackground(accent: document.accent)

            ScrollView {
                VStack(alignment: .leading, spacing: 28) {
                    header

                    ForEach(Array(document.sections.enumerated()), id: \.element.id) { index, section in
                        legalSection(section, number: index + 1)
                    }

                    DoodleRule(color: document.accent, lineWidth: 1.5)

                    Text("Last updated 21 July 2026")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .accessibilityIdentifier("legal.lastUpdated")
                }
                .frame(maxWidth: 820, alignment: .leading)
                .padding(.leading, 72)
                .padding(.trailing, 24)
                .padding(.top, 22)
                .padding(.bottom, 42)
                .frame(maxWidth: .infinity, alignment: .center)
            }
        }
        .navigationTitle(document.title)
        .navigationBarTitleDisplayMode(.inline)
        .accessibilityIdentifier("legal.\(document.rawValue).page")
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 11) {
            EyebrowLabel(text: document.eyebrow, color: document.accent)

            Text(document.title)
                .font(.custom("Noteworthy-Bold", size: 42, relativeTo: .largeTitle))
                .markerHighlight(document.accent)

            Text(document.introduction)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            Text(document == .privacy ? "NO TRACKING" : "PERSONAL STUDY")
                .font(.caption.monospaced().weight(.black))
                .foregroundStyle(document.accent)
                .padding(.top, 4)
                .rotationEffect(.degrees(-2))
        }
    }

    private func legalSection(_ section: LegalSectionContent, number: Int) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(String(format: "%02d", number))
                    .font(.caption.monospaced().weight(.black))
                    .foregroundStyle(document.accent)

                Text(section.title)
                    .font(.custom("Noteworthy-Bold", size: 23, relativeTo: .title3))
                    .foregroundStyle(.primary)
            }

            Text(section.body)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.leading, 34)

            DoodleRule(color: document.accent)
                .padding(.top, 6)
        }
        .accessibilityElement(children: .combine)
    }
}

#Preview("Privacy") {
    NavigationStack {
        LegalDocumentView(document: .privacy)
    }
}

#Preview("Terms") {
    NavigationStack {
        LegalDocumentView(document: .terms)
    }
}
