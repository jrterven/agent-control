import axe from "axe-core";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Button, Field, Switch } from "@hermes-control/ui";

describe("shared UI primitives", () => {
  it("exposes labeled, keyboard-friendly controls", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    const { container } = render(<div><Field label="Nombre" /><Switch checked={false} onChange={onChange} label="Caché offline" /><Button>Guardar</Button></div>);
    await user.click(screen.getByRole("switch", { name: "Caché offline" }));
    expect(onChange).toHaveBeenCalledWith(true);
    expect(screen.getByLabelText("Nombre")).toBeInTheDocument();
    expect((await axe.run(container)).violations).toHaveLength(0);
  });
});
