"""Synthetic window process for source and frozen capture acceptance."""


def cover_window(connection):
    """Show a separate-process occluder without application startup."""
    import tkinter as tk
    try:
        cover = tk.Tk()
        cover.geometry("280x220+80+80")
        cover.configure(bg="#ff0000")
        cover.attributes("-topmost", True)
        cover.update()
    except Exception as error:
        connection.send(type(error).__name__)
        return
    connection.send(True)
    try:
        while not connection.poll(0.01):
            cover.update()
    finally:
        cover.destroy()
        connection.close()
