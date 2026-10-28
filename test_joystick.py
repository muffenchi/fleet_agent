import pygame

def detect_joystick():
    pygame.init()
    pygame.joystick.init()

    joystick_count = pygame.joystick.get_count()

    if joystick_count == 0:
        print("No joystick detected. Please connect your USB joystick and try again.")
        return

    print(f"Detected {joystick_count} joystick(s).\n")

    for i in range(joystick_count):
        joystick = pygame.joystick.Joystick(i)
        joystick.init()

        name = joystick.get_name()
        num_axes = joystick.get_numaxes()
        num_buttons = joystick.get_numbuttons()
        num_hats = joystick.get_numhats()

        print(f"--- Joystick {i}: {name} ---")
        print(f"Number of Axes: {num_axes}")
        print(f"Number of Buttons: {num_buttons}")
        print(f"Number of Hats: {num_hats}\n")

        # Axes
        print("Axis Values (initial):")
        for a in range(num_axes):
            print(f"  Axis {a}: {joystick.get_axis(a):.2f}")

        # Buttons
        print("\nButton States (initial):")
        for b in range(num_buttons):
            state = joystick.get_button(b)
            print(f"  Button {b}: {'Pressed' if state else 'Released'}")

        # Hats (POV knobs / D-pads)
        print("\nHat States (initial):")
        if num_hats == 0:
            print("  (no hats detected)")
        else:
            for h in range(num_hats):
                hat_x, hat_y = joystick.get_hat(h)
                print(f"  Hat {h}: x={hat_x}, y={hat_y}")

        print("\n" + "-" * 30 + "\n")

    pygame.event.pump()
    input("Press Enter to exit...")
    pygame.quit()


if __name__ == "__main__":
    detect_joystick()