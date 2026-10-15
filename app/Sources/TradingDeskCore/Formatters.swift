import Foundation

/// Formato es-ES. Los importes con signo llevan siempre «+» o «−» además del color,
/// para que el color nunca sea el único indicador (PRD §4).
public enum Fmt {
    public static let locale = Locale(identifier: "es_ES")

    public static func money(_ value: Decimal, currency: String) -> String {
        currencyFormatter(currency).string(from: NSDecimalNumber(decimal: value)) ?? "\(value) \(currency)"
    }

    public static func money(_ money: Money) -> String {
        self.money(money.amount.value, currency: money.currency)
    }

    public static func signedMoney(_ value: Decimal, currency: String) -> String {
        sign(value) + self.money(abs(value), currency: currency)
    }

    public static func signedMoney(_ money: Money) -> String {
        signedMoney(money.amount.value, currency: money.currency)
    }

    public static func signedPercent(_ value: Decimal) -> String {
        sign(value) + number(abs(value), fractionDigits: 2) + " %"
    }

    public static func number(_ value: Decimal, fractionDigits: Int = 2) -> String {
        let formatter = NumberFormatter()
        formatter.locale = locale
        formatter.numberStyle = .decimal
        formatter.minimumFractionDigits = fractionDigits
        formatter.maximumFractionDigits = fractionDigits
        return formatter.string(from: NSDecimalNumber(decimal: value)) ?? "\(value)"
    }

    public static func sign(_ value: Decimal) -> String {
        value > 0 ? "+" : (value < 0 ? "−" : "")
    }

    /// Texto que acompaña al color: «Ganancia», «Pérdida» o «Sin cambios».
    public static func direction(_ value: Decimal) -> String {
        value > 0 ? "Ganancia" : (value < 0 ? "Pérdida" : "Sin cambios")
    }

    public static func dateTime(_ date: Date, timeZone: TimeZone = .current) -> String {
        let formatter = DateFormatter()
        formatter.locale = locale
        formatter.timeZone = timeZone
        formatter.dateFormat = "dd/MM/yyyy HH:mm:ss"
        return formatter.string(from: date)
    }

    private static func currencyFormatter(_ currency: String) -> NumberFormatter {
        let formatter = NumberFormatter()
        formatter.locale = locale
        formatter.numberStyle = .currency
        formatter.currencyCode = currency
        formatter.minimumFractionDigits = 2
        formatter.maximumFractionDigits = 2
        return formatter
    }
}
