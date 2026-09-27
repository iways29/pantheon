/** Two soft lights behind the glass: glass needs something to bend. */
export function Auras() {
  return (
    <>
      <div
        className="aura"
        aria-hidden="true"
        style={{
          right: -220,
          top: -300,
          width: 760,
          height: 760,
          background: 'radial-gradient(circle, var(--iceS) 0%, transparent 64%)',
        }}
      />
      <div
        className="aura"
        aria-hidden="true"
        style={{
          left: -260,
          bottom: -320,
          width: 800,
          height: 800,
          background: 'radial-gradient(circle, var(--goldS) 0%, transparent 64%)',
        }}
      />
    </>
  );
}
