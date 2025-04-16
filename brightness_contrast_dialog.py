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

        self.slider_brightness = self._create_slider()
        self.slider_contrast = self._create_slider()
        
        # Add numeric input fields for brightness and contrast
        self.spinbox_brightness = self._create_spinbox()
        self.spinbox_contrast = self._create_spinbox()
        
        # Connect sliders to spinboxes
        self.slider_brightness.valueChanged.connect(lambda value: self.spinbox_brightness.setValue(value))
        self.slider_contrast.valueChanged.connect(lambda value: self.spinbox_contrast.setValue(value))
        
        # Connect spinboxes to sliders
        self.spinbox_brightness.valueChanged.connect(lambda value: self.slider_brightness.setValue(value))
        self.spinbox_contrast.valueChanged.connect(lambda value: self.slider_contrast.setValue(value))

        # Create horizontal layouts for slider+spinbox pairs
        brightness_layout = QtWidgets.QHBoxLayout()
        brightness_layout.addWidget(self.slider_brightness)
        brightness_layout.addWidget(self.spinbox_brightness)
        
        contrast_layout = QtWidgets.QHBoxLayout()
        contrast_layout.addWidget(self.slider_contrast)
        contrast_layout.addWidget(self.spinbox_contrast)
        
        formLayout = QtWidgets.QFormLayout()
        formLayout.addRow(self.tr("Brightness"), brightness_layout)
        formLayout.addRow(self.tr("Contrast"), contrast_layout)
        self.setLayout(formLayout)

        assert isinstance(img, PIL.Image.Image)
        self.img = img
        self.callback = callback

    def onNewValue(self, value):
        brightness = self.slider_brightness.value() / 50.0
        contrast = self.slider_contrast.value() / 50.0

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
        spinbox.valueChanged.connect(self.onNewValue)
        return spinbox
