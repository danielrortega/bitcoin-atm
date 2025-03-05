import sys
from PyQt5.QtWidgets import QApplication
from atm_gui import BTMWindow
from atm_core import process_offline_queue
from utils import is_online

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = BTMWindow()
    window.showFullScreen()
    
    # Processa fila offline ao iniciar, se online
    if is_online():
        process_offline_queue()
    
    sys.exit(app.exec_())
