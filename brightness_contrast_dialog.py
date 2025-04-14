import PIL.Image
import PIL.ImageEnhance
from qtpy import QtGui
from qtpy import QtWidgets
from qtpy.QtCore import Qt

from .. import utils


class BrightnessContrastDialog(QtWidgets.QDialog):
    def __init__(self, img, callback, parent=None):
        super(BrightnessContrastDialog, self).__init__(parent)
        self.setModal(True)
        self.setWindowTitle("Brightness/Contrast")

        # Create sliders and spinboxes
        self.slider_brightness = self._create_slider()
        self.slider_contrast = self._create_slider()
        self.spinbox_brightness = self._create_spinbox()
        self.spinbox_contrast = self._create_spinbox()

        # Connect spinboxes to update sliders
        self.spinbox_brightness.valueChanged.connect(self._brightness_spinbox_changed)
        self.spinbox_contrast.valueChanged.connect(self._contrast_spinbox_changed)

        # Create layouts for each parameter with slider and spinbox side by side
        brightness_layout = QtWidgets.QHBoxLayout()
        brightness_layout.addWidget(self.slider_brightness)
        brightness_layout.addWidget(self.spinbox_brightness)
        
        contrast_layout = QtWidgets.QHBoxLayout()
        contrast_layout.addWidget(self.slider_contrast)
        contrast_layout.addWidget(self.spinbox_contrast)

        # Main form layout
        formLayout = QtWidgets.QFormLayout()
        formLayout.addRow(self.tr("Brightness"), brightness_layout)
        formLayout.addRow(self.tr("Contrast"), contrast_layout)
        self.setLayout(formLayout)

        assert isinstance(img, PIL.Image.Image)
        self.img = img
        self.callback = callback

    def _brightness_spinbox_changed(self, value):
        self.slider_brightness.blockSignals(True)
        self.slider_brightness.setValue(value)
        self.slider_brightness.blockSignals(False)
        self.onNewValue(value)

    def _contrast_spinbox_changed(self, value):
        self.slider_contrast.blockSignals(True)
        self.slider_contrast.setValue(value)
        self.slider_contrast.blockSignals(False)
        self.onNewValue(value)

    def onNewValue(self, value):
        brightness = self.slider_brightness.value() / 50.0
        contrast = self.slider_contrast.value() / 50.0

        # Update spinboxes if they don't match sliders
        if self.spinbox_brightness.value() != self.slider_brightness.value():
            self.spinbox_brightness.blockSignals(True)
            self.spinbox_brightness.setValue(self.slider_brightness.value())
            self.spinbox_brightness.blockSignals(False)
            
        if self.spinbox_contrast.value() != self.slider_contrast.value():
            self.spinbox_contrast.blockSignals(True)
            self.spinbox_contrast.setValue(self.slider_contrast.value())
            self.spinbox_contrast.blockSignals(False)

        img = self.img
        img = PIL.ImageEnhance.Brightness(img).enhance(brightness)
        img = PIL.ImageEnhance.Contrast(img).enhance(contrast)

        img_data = utils.img_pil_to_data(img)
        qimage = QtGui.QImage.fromData(img_data)
        self.callback(qimage)

    def _create_slider(self):
        slider = QtWidgets.QSlider(Qt.Horizontal)
        slider.setRange(0, 150)
        slider.setValue(50)
        slider.valueChanged.connect(self.onNewValue)
        return slider
        
    def _create_spinbox(self):
        spinbox = QtWidgets.QSpinBox()
        spinbox.setRange(0, 150)
        spinbox.setValue(50)
        spinbox.setSuffix("")
        spinbox.setFixedWidth(70)
        return spinbox
