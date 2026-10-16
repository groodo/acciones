import XCTest
@testable import TradingDeskCore

final class DecodingTests: XCTestCase {
    func testEnvelopeWithAccountDecodesDecimalsExactly() throws {
        let json = """
        {"source":"MOCK","is_mock":true,"mode":"paper","generated_at":"2026-10-15T17:58:15.134487Z",
         "notice":"DATOS SIMULADOS","data":{
          "account_id_masked":"DU*****123","mode":"paper","timestamp":"2026-10-15T17:58:15Z","base_currency":"EUR",
          "net_liquidation":{"amount":"1001.63","currency":"EUR"},
          "net_liquidation_eur":{"amount":"1001.63","currency":"EUR"},
          "fx":{"pair":"EUR.USD","rate":"1.0850","source":"MOCK","as_of":"2026-10-15T17:58:15.1+00:00"},
          "settled_cash":[{"amount":"598.50","currency":"EUR"}],
          "available_funds":{"amount":"598.50","currency":"EUR"},
          "buying_power":{"amount":"598.50","currency":"EUR"},
          "unrealized_pnl":{"amount":"1.29","currency":"EUR"},
          "realized_pnl":{"amount":"3.20","currency":"EUR"},
          "realized_fees":{"amount":"2.00","currency":"EUR"},
          "daily_pnl":{"amount":"0.31","currency":"EUR"},
          "daily_pnl_pct":"0.03","daily_pnl_basis":"x",
          "invested_capital":{"amount":"401.85","currency":"EUR"},
          "exposure":{"amount":"403.13","currency":"EUR"},
          "risk_budget":{"amount":"5.00","currency":"EUR"},
          "freshness":"simuladas"}}
        """
        let env = try JSON.decoder().decode(Envelope<AccountSnapshot>.self, from: Data(json.utf8))
        XCTAssertTrue(env.isMock)
        XCTAssertEqual(env.source, "MOCK")
        XCTAssertEqual(env.data.netLiquidationEur.amount.value, Decimal(string: "1001.63"))
        XCTAssertEqual(env.data.fx?.rate.value, Decimal(string: "1.0850"))
        XCTAssertEqual(env.data.freshness, .simulated)
    }

    func testRejectsNonNumericAmount() {
        let json = #"{"amount":"abc","currency":"EUR"}"#
        XCTAssertThrowsError(try JSON.decoder().decode(Money.self, from: Data(json.utf8)))
    }

    func testDatesWithAndWithoutFraction() {
        XCTAssertNotNil(JSON.parseDate("2026-10-15T17:58:15.134487Z"))
        XCTAssertNotNil(JSON.parseDate("2026-10-15T17:58:15Z"))
        XCTAssertNotNil(JSON.parseDate("2026-10-15T17:58:15+00:00"))
        XCTAssertEqual(JSON.parseDate("2026-10-15T17:58:15.5Z")!.timeIntervalSince(JSON.parseDate("2026-10-15T17:58:15Z")!), 0.5, accuracy: 0.001)
        XCTAssertNil(JSON.parseDate("ayer"))
    }

    func testSignalWithoutDataHasNoEvaluation() throws {
        let json = """
        {"symbol":"NVDA","name":"NVIDIA","price":null,"change_pct":null,"freshness":"diferidas","trend":"—",
         "proposal":"NO_EVALUABLE","horizon_minutes":5,"net_eval_after_costs":null,"status":"BLOQUEADA",
         "reasons":["Sin cotizaciones"],"contradictions":[],"model_version":null}
        """
        let signal = try JSON.decoder().decode(Signal.self, from: Data(json.utf8))
        XCTAssertNil(signal.price)
        XCTAssertNil(signal.netEvalAfterCosts)
        XCTAssertEqual(signal.proposal.label, "No evaluable")
    }
}

final class FormatTests: XCTestCase {
    func testSignedMoneyAlwaysCarriesSign() {
        let gain = Fmt.signedMoney(Decimal(string: "250.5")!, currency: "EUR")
        let loss = Fmt.signedMoney(Decimal(-10), currency: "EUR")
        XCTAssertTrue(gain.hasPrefix("+"))
        XCTAssertTrue(gain.contains("250,50"))
        XCTAssertTrue(gain.hasSuffix("€"))
        XCTAssertTrue(loss.hasPrefix("−"))
        XCTAssertTrue(loss.contains("10,00"))
        XCTAssertEqual(Fmt.sign(0), "")
    }

    func testDirectionTextAccompaniesColour() {
        XCTAssertEqual(Fmt.direction(1), "Ganancia")
        XCTAssertEqual(Fmt.direction(-1), "Pérdida")
        XCTAssertEqual(Fmt.direction(0), "Sin cambios")
    }

    func testPercent() {
        XCTAssertEqual(Fmt.signedPercent(Decimal(string: "0.54")!), "+0,54 %")
    }
}

final class ClientTests: XCTestCase {
    struct StubTransport: HTTPTransport {
        let status: Int
        let body: String
        func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
            (Data(body.utf8), HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
        }
    }

    let config = ServiceConfig(baseURL: URL(string: "http://127.0.0.1:8765")!, tokenPath: URL(fileURLWithPath: "/nonexistent"))

    func testRequestCarriesBearerTokenAndLocalhost() throws {
        let client = APIClient(config: config, transport: StubTransport(status: 200, body: "{}"), tokenProvider: { "abc" })
        let request = try client.makeRequest("GET", "/v1/status", body: nil)
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer abc")
        XCTAssertEqual(request.url?.absoluteString, "http://127.0.0.1:8765/v1/status")
    }

    func testMissingTokenIsReported() async {
        let client = APIClient(config: config, transport: StubTransport(status: 200, body: "{}"))
        do {
            _ = try await client.status()
            XCTFail("debería fallar")
        } catch {
            XCTAssertEqual(error as? APIError, .missingToken("/nonexistent"))
        }
    }

    func testModelStatusDecodes() async throws {
        let body = #"{"installed":true,"loaded":true,"model_id":"NeoQuasar/Kronos-small","version":"v1","device":"mps","detail":"Cargado","last_latency_ms":5500,"last_run":"2026-10-16T16:30:00.123456+00:00","watchlist":["AAPL","SPY"]}"#
        let client = APIClient(config: config, transport: StubTransport(status: 200, body: body), tokenProvider: { "abc" })
        let status = try await client.modelStatus()
        XCTAssertEqual(status.summary, "Funcionando (mps)")
        XCTAssertEqual(status.lastLatencyMs, 5500)
        XCTAssertNotNil(status.lastRun)
    }

    func testKillSwitchBlockSurfacesMessage() async {
        let body = #"{"detail":"Operativa detenida: no se aceptan nuevas entradas."}"#
        let client = APIClient(config: config, transport: StubTransport(status: 423, body: body), tokenProvider: { "abc" })
        do {
            _ = try await client.createSimulatedOrder(SimulatedOrderRequest(symbol: "MSFT", quantity: 1, limitPrice: 418))
            XCTFail("debería fallar")
        } catch {
            XCTAssertEqual(error as? APIError, .blocked("Operativa detenida: no se aceptan nuevas entradas."))
        }
    }

    func testSimulatedOrderEncodesSnakeCaseStrings() throws {
        let req = SimulatedOrderRequest(symbol: "MSFT", quantity: 1, limitPrice: Decimal(string: "418.2")!, clientOrderId: "UI-TEST0001")
        let object = try JSONSerialization.jsonObject(with: JSON.encoder().encode(req)) as! [String: String]
        XCTAssertEqual(object["client_order_id"], "UI-TEST0001")
        XCTAssertEqual(object["limit_price"], "418.2")
        XCTAssertTrue(SimulatedOrderRequest.newClientOrderId().hasPrefix("UI-"))
    }

    func testOrderStateTerminality() {
        XCTAssertTrue(OrderState.filled.isTerminal)
        XCTAssertFalse(OrderState.unknown.isTerminal)
    }
}
