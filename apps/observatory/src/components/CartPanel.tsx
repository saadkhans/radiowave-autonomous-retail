import { fmtSeconds } from "@/lib/format";
import { useActions, useObservatory } from "@/state/store";
import type { Cart } from "@/types/api";

function CartCard({ cart }: { cart: Cart }) {
  const { selection } = useObservatory();
  const { select } = useActions();
  const selected = selection?.kind === "person" && selection.id === cart.shopper_track_id;
  return (
    <div
      className={`mb-2 rounded border px-2 py-1.5 ${
        selected ? "border-console-accent" : "border-console-line"
      }`}
      data-testid={`cart-${cart.cart_id}`}
    >
      <div className="flex items-center gap-2">
        <button
          type="button"
          className="mono text-[12px] text-console-accent hover:underline"
          onClick={() => select({ kind: "person", id: cart.shopper_track_id })}
        >
          {cart.shopper_track_id}
        </button>
        <span className="mono text-[10px] text-console-muted">{cart.session_id ?? "no session"}</span>
        <div className="flex-1" />
        <span
          className="badge"
          style={{
            background: cart.status === "OPEN" ? "#1f3b2c" : "#2f2340",
            color: cart.status === "OPEN" ? "#5ad48f" : "#c792ff",
          }}
        >
          {cart.status}
          {cart.exited_s !== null ? ` @ ${cart.exited_s.toFixed(1)}s` : ""}
        </span>
      </div>
      {cart.lines.length === 0 ? (
        <div className="mono text-[11px] text-console-muted">empty</div>
      ) : (
        <table className="mono w-full text-[11px]">
          <tbody>
            {cart.lines.map((line) => (
              <tr key={line.epc} className="cursor-pointer hover:bg-console-panel-2">
                <td className="py-0.5 pr-2" onClick={() => select({ kind: "item", id: line.epc })}>
                  {line.short_epc}
                </td>
                <td className="py-0.5 pr-2 text-console-text/90">
                  {line.product_name ?? "—"}
                  <span className="text-console-muted"> {line.gtin ?? ""}</span>
                </td>
                <td className="py-0.5 pr-2 text-console-muted">added {fmtSeconds(line.added_s)}</td>
                <td className="py-0.5 text-right">
                  {cart.status === "EXITED" && line.exit_event_s !== null ? (
                    <span className="text-console-ok">exited {fmtSeconds(line.exit_event_s)}</span>
                  ) : line.final_ownership_candidate || line.exit_event_s !== null ? (
                    // The cart is still open: the exit hold is provisional ownership,
                    // not a settled exit.
                    <span className="text-console-warn">
                      exit candidate{line.exit_event_s !== null ? ` @ ${fmtSeconds(line.exit_event_s)}` : ""}
                    </span>
                  ) : (
                    <span className="text-console-muted">in cart</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function CartPanel() {
  const { run } = useObservatory();
  const carts = run?.carts ?? [];
  const open = carts.filter((cart) => cart.status === "OPEN");
  const exited = carts.filter((cart) => cart.status !== "OPEN");
  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="cart-panel">
      <div className="panel-title border-b border-console-line px-3 py-2">
        Virtual carts · {open.length} open · {exited.length} exited
      </div>
      <div className="scroll-y min-h-0 flex-1 px-3 py-2">
        {carts.length === 0 ? <div className="text-console-muted">No carts.</div> : null}
        {open.map((cart) => (
          <CartCard key={cart.cart_id} cart={cart} />
        ))}
        {exited.length > 0 ? <div className="panel-title mb-1 mt-2">Exited</div> : null}
        {exited.map((cart) => (
          <CartCard key={cart.cart_id} cart={cart} />
        ))}
      </div>
    </div>
  );
}
