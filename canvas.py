import imgviz
from qtpy import QtCore
from qtpy import QtGui
from qtpy import QtWidgets

import labelme.ai
import labelme.utils
from labelme import QT5
from labelme.logger import logger
from labelme.shape import Shape
from qtpy.QtCore import Qt, QPoint
from qtpy.QtGui import QPainter, QColor, QPen, QPixmap
from labelme.utils.shape import shape_to_mask
from collections import defaultdict
import numpy as np
# TODO(unknown):
# - [maybe] Find optimal epsilon value.


CURSOR_DEFAULT = QtCore.Qt.ArrowCursor
CURSOR_POINT = QtCore.Qt.PointingHandCursor
CURSOR_DRAW = QtCore.Qt.CrossCursor
CURSOR_MOVE = QtCore.Qt.ClosedHandCursor
CURSOR_GRAB = QtCore.Qt.OpenHandCursor

MOVE_SPEED = 5.0


class Canvas(QtWidgets.QWidget):
    zoomRequest = QtCore.Signal(int, QtCore.QPoint)
    scrollRequest = QtCore.Signal(int, int)
    newShape = QtCore.Signal(str)
    selectionChanged = QtCore.Signal(list)
    shapeMoved = QtCore.Signal()
    drawingPolygon = QtCore.Signal(bool)
    vertexSelected = QtCore.Signal(bool)
    classAndIntensityChanged = QtCore.Signal(str, str)
    mouseBackButtonClicked = QtCore.Signal()
    reset_masklabel=QtCore.Signal()
    copy_masklabel=QtCore.Signal()
    paste_masklabel=QtCore.Signal()

    
    CREATE, EDIT = 0, 1
    
    # polygon, rectangle, line, or point
    _createMode = "patch_annotation"
    _fill_drawing = False
    temp_mask_data=None
    temp_shape_data=None
    box_start_point = None  # To store the starting point for box annotation
    box_annotation_mode = False  # Flag to indicate box annotation mode

    def __init__(self, *args, **kwargs):
        self.epsilon = kwargs.pop("epsilon", 10.0)
        self.double_click = kwargs.pop("double_click", "close")
        if self.double_click not in [None, "close"]:
            raise ValueError(
                "Unexpected value for double_click event: {}".format(self.double_click)
            )
        self.num_backups = kwargs.pop("num_backups", 10)
        self._crosshair = kwargs.pop(
            "crosshair",
            {
                "polygon": False,
                "rectangle": True,
                "circle": False,
                "line": False,
                "point": False,
                "linestrip": False,
                "ai_polygon": False,
                "ai_mask": False,
            },
        )
        super(Canvas, self).__init__(*args, **kwargs)
        # Initialise local state.
        self.mode = self.EDIT
        self.shapes = []
        self.shapesBackups = []
        self.current = None
        self.selectedShapes = []  # save the selected shapes here
        self.selectedShapesCopy = []
        # self.line represents:
        #   - createMode == 'polygon': edge from last point to current
        #   - createMode == 'rectangle': diagonal line of the rectangle
        #   - createMode == 'line': the line
        #   - createMode == 'point': the point
        self.line = Shape()
        self.prevPoint = QtCore.QPoint()
        self.prevMovePoint = QtCore.QPoint()
        self.offsets = QtCore.QPoint(), QtCore.QPoint()
        self.scale = 1.0
        self.pixmap = QtGui.QPixmap()
        self.visible = {}
        self._hideBackround = False
        self.hideBackround = False
        self.hShape = None
        self.prevhShape = None
        self.hVertex = None
        self.prevhVertex = None
        self.hEdge = None
        self.prevhEdge = None
        self.movingShape = False
        self.snapping = True
        self.hShapeIsSelected = False
        self._painter = QtGui.QPainter()
        self._cursor = CURSOR_DEFAULT
        # Menus:
        # 0: right-click without selection and dragging of shapes
        # 1: right-click with selection and dragging of shapes
        self.menus = (QtWidgets.QMenu(), QtWidgets.QMenu())
        # Set widget options.
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.WheelFocus)

        self._ai_model = None

        self.patch_width = 16
        self.patch_height = 16
        self.previous_masks = {}
        self.shapes_visible = True
        self.class_text = None
        self.intensity_text = None
        self.tmp_class_text = None
        self.tmp_intensity_text = None

    def fillDrawing(self):
        return self._fill_drawing

    def setFillDrawing(self, value):
        self._fill_drawing = value

    @property
    def createMode(self):
        return self._createMode

    @createMode.setter
    def createMode(self, value):
        if value not in [
            "polygon",
            "rectangle",
            "circle",
            "line",
            "point",
            "linestrip",
            "ai_polygon",
            "ai_mask",
            "patch_annotation",
        ]:
            raise ValueError("Unsupported createMode: %s" % value)
        self._createMode = value

    def initializeAiModel(self, name):
        if name not in [model.name for model in labelme.ai.MODELS]:
            raise ValueError("Unsupported ai model: %s" % name)
        model = [model for model in labelme.ai.MODELS if model.name == name][0]

        if self._ai_model is not None and self._ai_model.name == model.name:
            logger.debug("AI model is already initialized: %r" % model.name)
        else:
            logger.debug("Initializing AI model: %r" % model.name)
            self._ai_model = model()

        if self.pixmap is None:
            logger.warning("Pixmap is not set yet")
            return

        self._ai_model.set_image(
            image=labelme.utils.img_qt_to_arr(self.pixmap.toImage())
        )

    def storeMaskLabel(self):
        self.mask_label_backup = [row[:] for row in self.mask_label]

    def restoreMaskLabel(self):
        self.mask_label = [row[:] for row in self.mask_label_backup]

    def storeShapes(self):
        shapesBackup = []
        for shape in self.shapes:
            shapesBackup.append(shape.copy())
        if len(self.shapesBackups) > self.num_backups:
            self.shapesBackups = self.shapesBackups[-self.num_backups - 1 :]
        # self.shapesBackups.append(shapesBackup)
        self.shapesBackups.append((shapesBackup, [row[:] for row in self.mask_label]))
        self.storeMaskLabel()

    @property
    def isShapeRestorable(self):
        # We save the state AFTER each edit (not before) so for an
        # edit to be undoable, we expect the CURRENT and the PREVIOUS state
        # to be in the undo stack.
        if len(self.shapesBackups) < 2:
            return False
        return True

    def restoreShape(self):
        # This does _part_ of the job of restoring shapes.
        # The complete process is also done in app.py::undoShapeEdit
        # and app.py::loadShapes and our own Canvas::loadShapes function.
        if not self.isShapeRestorable:
            return
        self.shapesBackups.pop()  # latest

        # The application will eventually call Canvas.loadShapes which will
        # push this right back onto the stack.
        # shapesBackup = self.shapesBackups.pop()
        shapesBackup, mask_label_backup = self.shapesBackups.pop()
        self.shapes = shapesBackup
        self.mask_label = mask_label_backup
        self.selectedShapes = []
        for shape in self.shapes:
            shape.selected = False
        #self.update()

    def enterEvent(self, ev):
        self.overrideCursor(self._cursor)

    def leaveEvent(self, ev):
        self.unHighlight()
        self.restoreCursor()

    def focusOutEvent(self, ev):
        self.restoreCursor()

    def isVisible(self, shape):
        return self.visible.get(shape, True)

    def drawing(self):
        return self.mode == self.CREATE

    def editing(self):
        return self.mode == self.EDIT

    def setEditing(self, value=True):
        self.mode = self.EDIT if value else self.CREATE
        if self.mode == self.EDIT:
            # CREATE -> EDIT
            self.repaint()  # clear crosshair
        else:
            # EDIT -> CREATE
            self.unHighlight()
            self.deSelectShape()

    def unHighlight(self):
        if self.hShape:
            self.hShape.highlightClear()
            #self.update()
        self.prevhShape = self.hShape
        self.prevhVertex = self.hVertex
        self.prevhEdge = self.hEdge
        self.hShape = self.hVertex = self.hEdge = None

    def selectedVertex(self):
        return self.hVertex is not None

    def selectedEdge(self):
        return self.hEdge is not None

    def mouseMoveEvent(self, ev):
        """Update line with last point and current coordinates."""
        try:
            if QT5:
                pos = self.transformPos(ev.localPos())
            else:
                pos = self.transformPos(ev.posF())
        except AttributeError:
            return

        self.prevMovePoint = pos
        self.restoreCursor()

        is_shift_pressed = ev.modifiers() & QtCore.Qt.ShiftModifier

        # Box annotation mode with Shift+RightClick drag
        if self.box_annotation_mode and self.box_start_point is not None:
            self.overrideCursor(CURSOR_DRAW)
            self.line.shape_type = "rectangle"
            self.line.points = [self.box_start_point, pos]
            self.line.point_labels = [1, 1]
            self.update()
            return

        if self.drawing() and self.createMode == "patch_annotation" and is_shift_pressed:
            self.overrideCursor(CURSOR_DRAW)
            if not self.current:
                #self.repaint()
                return

            if self.outOfPixmap(pos):
                pos = self.intersectionPoint(self.current[-1], pos)

            self.current.addPoint(pos)
            #self.repaint()
            return
        
        # Polygon drawing.
        if self.drawing():
            if self.createMode in ["ai_polygon", "ai_mask"]:
                self.line.shape_type = "points"
            else:
                self.line.shape_type = self.createMode

            self.overrideCursor(CURSOR_DRAW)
            if not self.current:
                #self.repaint()  # draw crosshair
                return

            if self.outOfPixmap(pos):
                # Don't allow the user to draw outside the pixmap.
                # Project the point to the pixmap's edges.
                pos = self.intersectionPoint(self.current[-1], pos)
            elif (
                self.snapping
                and len(self.current) > 1
                and self.createMode == "polygon"
                and self.closeEnough(pos, self.current[0])
            ):
                # Attract line to starting point and
                # colorise to alert the user.
                pos = self.current[0]
                self.overrideCursor(CURSOR_POINT)
                self.current.highlightVertex(0, Shape.NEAR_VERTEX)


            if self.createMode in ["polygon", "linestrip"]:
                self.line.points = [self.current[-1], pos]
                self.line.point_labels = [1, 1]
            elif self.createMode in ["ai_polygon", "ai_mask"]:
                self.line.points = [self.current.points[-1], pos]
                self.line.point_labels = [
                    self.current.point_labels[-1],
                    0 if is_shift_pressed else 1,
                ]
            elif self.createMode == "rectangle":
                self.line.points = [self.current[0], pos]
                self.line.point_labels = [1, 1]
                self.line.close()
            elif self.createMode == "circle":
                self.line.points = [self.current[0], pos]
                self.line.point_labels = [1, 1]
                self.line.shape_type = "circle"
            elif self.createMode == "line":
                self.line.points = [self.current[0], pos]
                self.line.point_labels = [1, 1]
                self.line.close()
            elif self.createMode == "point":
                self.line.points = [self.current[0]]
                self.line.point_labels = [1]
                self.line.close()
            elif self.createMode == "patch_annotation" and is_shift_pressed:
                self.current.addPoint(pos, label=1)  # Add points while dragging
                #self.update()

                
            assert len(self.line.points) == len(self.line.point_labels)
            #self.repaint()
            self.current.highlightClear()
            return

        # Polygon copy moving.
        if QtCore.Qt.RightButton & ev.buttons():
            if self.selectedShapesCopy and self.prevPoint:
                self.overrideCursor(CURSOR_MOVE)
                self.boundedMoveShapes(self.selectedShapesCopy, pos)
                #self.repaint()
            elif self.selectedShapes:
                self.selectedShapesCopy = [s.copy() for s in self.selectedShapes]
                #self.repaint()
            return

        # Polygon/Vertex moving.
        if QtCore.Qt.LeftButton & ev.buttons():
            if self.selectedVertex():
                self.boundedMoveVertex(pos)
                #self.repaint()
                self.movingShape = True
            elif self.selectedShapes and self.prevPoint:
                self.overrideCursor(CURSOR_MOVE)
                self.boundedMoveShapes(self.selectedShapes, pos)
                #self.repaint()
                self.movingShape = True
            return

        # Just hovering over the canvas, 2 possibilities:
        # - Highlight shapes
        # - Highlight vertex
        # Update shape/vertex fill and tooltip value accordingly.
        self.setToolTip(self.tr("Image"))
        for shape in reversed([s for s in self.shapes if self.isVisible(s)]):
            # Look for a nearby vertex to highlight. If that fails,
            # check if we happen to be inside a shape.
            index = shape.nearestVertex(pos, self.epsilon / self.scale)
            index_edge = shape.nearestEdge(pos, self.epsilon / self.scale)
            if index is not None:
                if self.selectedVertex():
                    self.hShape.highlightClear()
                self.prevhVertex = self.hVertex = index
                self.prevhShape = self.hShape = shape
                self.prevhEdge = self.hEdge
                self.hEdge = None
                shape.highlightVertex(index, shape.MOVE_VERTEX)
                self.overrideCursor(CURSOR_POINT)
                self.setToolTip(self.tr("Click & drag to move point"))
                self.setStatusTip(self.toolTip())
                self.update()
                break
            elif index_edge is not None and shape.canAddPoint():
                if self.selectedVertex():
                    self.hShape.highlightClear()
                self.prevhVertex = self.hVertex
                self.hVertex = None
                self.prevhShape = self.hShape = shape
                self.prevhEdge = self.hEdge = index_edge
                self.overrideCursor(CURSOR_POINT)
                self.setToolTip(self.tr("Click to create point"))
                self.setStatusTip(self.toolTip())
                self.update()
                break
            elif shape.containsPoint(pos):
                if self.selectedVertex():
                    self.hShape.highlightClear()
                self.prevhVertex = self.hVertex
                self.hVertex = None
                self.prevhShape = self.hShape = shape
                self.prevhEdge = self.hEdge
                self.hEdge = None
                self.setToolTip(
                    self.tr("Click & drag to move shape '%s'") % shape.label
                )
                self.setStatusTip(self.toolTip())
                self.overrideCursor(CURSOR_GRAB)
                self.update()
                break
        else:  # Nothing found, clear highlights, reset state.
            self.unHighlight()
        self.vertexSelected.emit(self.hVertex is not None)

    def addPointToEdge(self):
        shape = self.prevhShape
        index = self.prevhEdge
        point = self.prevMovePoint
        if shape is None or index is None or point is None:
            return
        shape.insertPoint(index, point)
        shape.highlightVertex(index, shape.MOVE_VERTEX)
        self.hShape = shape
        self.hVertex = index
        self.hEdge = None
        self.movingShape = True

    def removeSelectedPoint(self):
        shape = self.prevhShape
        index = self.prevhVertex
        if shape is None or index is None:
            return
        shape.removePoint(index)
        shape.highlightClear()
        self.hShape = shape
        self.prevhVertex = None
        self.movingShape = True  # Save changes

    def mousePressEvent(self, ev):
        if QT5:
            pos = self.transformPos(ev.localPos())
        else:
            pos = self.transformPos(ev.posF())
        if ev.button() == QtCore.Qt.XButton1:
            self.mouseBackButtonClicked.emit()
        is_shift_pressed = ev.modifiers() & QtCore.Qt.ShiftModifier
        
        # Start box annotation mode with Shift+RightClick
        if ev.button() == QtCore.Qt.RightButton and is_shift_pressed:
            # Initialize box annotation mode
            self.box_start_point = pos
            self.box_annotation_mode = True
            self.line.shape_type = "rectangle"
            self.line.points = [pos, pos]
            self.line.point_labels = [1, 1]
            return
        
        if ev.button() == QtCore.Qt.LeftButton:
            if self.drawing():
                if self.current:
                    # Add point to existing shape.
                    if self.createMode == "polygon":
                        self.current.addPoint(self.line[1])
                        self.line[0] = self.current[-1]
                        if self.current.isClosed():
                            self.finalise()
                    elif self.createMode in ["rectangle", "circle", "line"]:
                        assert len(self.current.points) == 1
                        self.current.points = self.line.points
                        self.finalise()
                    elif self.createMode == "linestrip":
                        self.current.addPoint(self.line[1])
                        self.line[0] = self.current[-1]
                        if int(ev.modifiers()) == QtCore.Qt.ControlModifier:
                            self.finalise()
                    elif self.createMode in ["ai_polygon", "ai_mask"]:
                        self.current.addPoint(
                            self.line.points[1],
                            label=self.line.point_labels[1],
                        )
                        self.line.points[0] = self.current.points[-1]
                        self.line.point_labels[0] = self.current.point_labels[-1]
                        if ev.modifiers() & QtCore.Qt.ControlModifier:
                            self.finalise()
                    elif self.createMode == "patch_annotation" and is_shift_pressed:
                        self.current.addPoint(self.line[1])
                        self.line[0] = self.current[-1]

                elif not self.outOfPixmap(pos):
                    # Create new shape.
                    self.current = Shape(
                        shape_type="points"
                        if self.createMode in ["ai_polygon", "ai_mask"]
                        else self.createMode
                    )
                    self.current.addPoint(pos, label=0 if is_shift_pressed else 1)
                    if self.createMode == "point":
                        self.finalise()
                    if self.createMode =="patch_annotation":
                        if is_shift_pressed:
                            self.line.points = [pos, pos]
                            self.line.point_labels = [1, 1]
                            self.setHiding()
                            self.drawingPolygon.emit(True)
                        else:
                            self.finalise()
                    elif (
                        self.createMode in ["ai_polygon", "ai_mask"]
                        and ev.modifiers() & QtCore.Qt.ControlModifier
                    ):
                        self.finalise()
                    else:
                        if self.createMode == "circle":
                            self.current.shape_type = "circle"
                        self.line.points = [pos, pos]
                        if (
                            self.createMode in ["ai_polygon", "ai_mask"]
                            and is_shift_pressed
                        ):
                            self.line.point_labels = [0, 0]
                        else:
                            self.line.point_labels = [1, 1]
                        self.setHiding()
                        self.drawingPolygon.emit(True)
                        self.update()
            elif self.editing():
                if self.selectedEdge():
                    self.addPointToEdge()
                elif (
                    self.selectedVertex()
                    and int(ev.modifiers()) == QtCore.Qt.ShiftModifier
                ):
                    # Delete point if: left-click + SHIFT on a point
                    self.removeSelectedPoint()

                group_mode = int(ev.modifiers()) == QtCore.Qt.ControlModifier
                self.selectShapePoint(pos, multiple_selection_mode=group_mode)
                self.prevPoint = pos
                #self.repaint()
        elif ev.button() == QtCore.Qt.RightButton and self.editing():
            group_mode = int(ev.modifiers()) == QtCore.Qt.ControlModifier
            if not self.selectedShapes or (
                self.hShape is not None and self.hShape not in self.selectedShapes
            ):
                self.selectShapePoint(pos, multiple_selection_mode=group_mode)
                #self.repaint()
            self.prevPoint = pos

    # def updatePatchSize(self, width, height):
    #     self.patch_width = width
    #     self.patch_height = height
    #     self.updateGrid()

    def update_patch_size(self, patch_width, patch_height):
        self.patch_width = patch_width
        self.patch_height = patch_height
        self.mask_label = self.initialize_mask(self.patch_width, self.patch_height)
        self.update()

    def drawGridOnPixmap(self):
        if not self.pixmap:
            return

        painter = QPainter(self.pixmap)
        pen = QPen(QColor(0, 255, 0), 3, Qt.SolidLine) 
        painter.setPen(pen)

        width = self.pixmap.width()
        height = self.pixmap.height()
        h_step = width // self.patch_width
        v_step = height // self.patch_height
        # self.debug_trace()
        for i in range(1, self.patch_width):
            painter.drawLine(QPoint(i * h_step, 0), QPoint(i * h_step, height))
        for i in range(1, self.patch_height):
            painter.drawLine(QPoint(0, i * v_step), QPoint(width, i * v_step))

        painter.end()

    def mouseReleaseEvent(self, ev):
        # Handle box annotation mode release with Shift+RightClick
        if self.box_annotation_mode and self.box_start_point is not None:
            # Only process if it was a right button release
            if ev.button() == QtCore.Qt.RightButton:
                end_point = self.transformPos(ev.localPos() if QT5 else ev.posF())
                # Create and annotate with box
                self.annotateWithBox(self.box_start_point, end_point)
                
                # Reset box annotation mode
                self.box_annotation_mode = False
                self.box_start_point = None
                self.update()
                return
                
        if ev.button() == QtCore.Qt.RightButton:
            menu = self.menus[len(self.selectedShapesCopy) > 0]
            self.restoreCursor()
            if not menu.exec_(self.mapToGlobal(ev.pos())) and self.selectedShapesCopy:
                # Cancel the move by deleting the shadow copy.
                self.selectedShapesCopy = []
                #self.repaint()
        elif ev.button() == QtCore.Qt.LeftButton:
            if self.editing():
                if (
                    self.hShape is not None
                    and self.hShapeIsSelected
                    and not self.movingShape
                ):
                    self.selectionChanged.emit(
                        [x for x in self.selectedShapes if x != self.hShape]
                    )

        if self.movingShape and self.hShape:
            index = self.shapes.index(self.hShape)
            if self.shapesBackups[-1][index].points != self.shapes[index].points:
                self.storeShapes()
                self.shapeMoved.emit()

            self.movingShape = False

    def endMove(self, copy):
        assert self.selectedShapes and self.selectedShapesCopy
        assert len(self.selectedShapesCopy) == len(self.selectedShapes)
        if copy:
            for i, shape in enumerate(self.selectedShapesCopy):
                self.shapes.append(shape)
                self.selectedShapes[i].selected = False
                self.selectedShapes[i] = shape
        else:
            for i, shape in enumerate(self.selectedShapesCopy):
                self.selectedShapes[i].points = shape.points
        self.selectedShapesCopy = []
        #self.repaint()
        self.storeShapes()
        return True

    def hideBackroundShapes(self, value):
        self.hideBackround = value
        if self.selectedShapes:
            # Only hide other shapes if there is a current selection.
            # Otherwise the user will not be able to select a shape.
            self.setHiding(True)
            self.update()

    def setHiding(self, enable=True):
        self._hideBackround = self.hideBackround if enable else False

    def canCloseShape(self):
        return self.drawing() and self.current and len(self.current) > 2

    def mouseDoubleClickEvent(self, ev):
        if self.double_click != "close":
            return

        if (
            self.createMode == "polygon" and self.canCloseShape()
        ) or self.createMode in ["ai_polygon", "ai_mask"]:
            self.finalise()

    def selectShapes(self, shapes):
        self.setHiding()
        self.selectionChanged.emit(shapes)
        self.update()

    def selectShapePoint(self, point, multiple_selection_mode):
        """Select the first shape created which contains this point."""
        if self.selectedVertex():  # A vertex is marked for selection.
            index, shape = self.hVertex, self.hShape
            shape.highlightVertex(index, shape.MOVE_VERTEX)
        else:
            for shape in reversed(self.shapes):
                if self.isVisible(shape) and shape.containsPoint(point):
                    self.setHiding()
                    if shape not in self.selectedShapes:
                        if multiple_selection_mode:
                            self.selectionChanged.emit(self.selectedShapes + [shape])
                        else:
                            self.selectionChanged.emit([shape])
                        self.hShapeIsSelected = False
                    else:
                        self.hShapeIsSelected = True
                    self.calculateOffsets(point)
                    return
        self.deSelectShape()

    def calculateOffsets(self, point):
        left = self.pixmap.width() - 1
        right = 0
        top = self.pixmap.height() - 1
        bottom = 0
        for s in self.selectedShapes:
            rect = s.boundingRect()
            if rect.left() < left:
                left = rect.left()
            if rect.right() > right:
                right = rect.right()
            if rect.top() < top:
                top = rect.top()
            if rect.bottom() > bottom:
                bottom = rect.bottom()

        x1 = left - point.x()
        y1 = top - point.y()
        x2 = right - point.x()
        y2 = bottom - point.y()
        self.offsets = QtCore.QPointF(x1, y1), QtCore.QPointF(x2, y2)

    def boundedMoveVertex(self, pos):
        index, shape = self.hVertex, self.hShape
        point = shape[index]
        if self.outOfPixmap(pos):
            pos = self.intersectionPoint(point, pos)
        shape.moveVertexBy(index, pos - point)

    def boundedMoveShapes(self, shapes, pos):
        if self.outOfPixmap(pos):
            return False  # No need to move
        o1 = pos + self.offsets[0]
        if self.outOfPixmap(o1):
            pos -= QtCore.QPointF(min(0, o1.x()), min(0, o1.y()))
        o2 = pos + self.offsets[1]
        if self.outOfPixmap(o2):
            pos += QtCore.QPointF(
                min(0, self.pixmap.width() - o2.x()),
                min(0, self.pixmap.height() - o2.y()),
            )
        # XXX: The next line tracks the new position of the cursor
        # relative to the shape, but also results in making it
        # a bit "shaky" when nearing the border and allows it to
        # go outside of the shape's area for some reason.
        # self.calculateOffsets(self.selectedShapes, pos)
        dp = pos - self.prevPoint
        if dp:
            for shape in shapes:
                shape.moveBy(dp)
            self.prevPoint = pos
            return True
        return False

    def deSelectShape(self):
        if self.selectedShapes:
            self.setHiding(False)
            self.selectionChanged.emit([])
            self.hShapeIsSelected = False
            self.update()

    def deleteSelected(self):
        deleted_shapes = []
        if self.selectedShapes:
            for shape in self.selectedShapes:
                self.shapes.remove(shape)
                deleted_shapes.append(shape)
            self.storeShapes()
            self.selectedShapes = []
            self.update()
        return deleted_shapes

    def deleteShape(self, shape):
        if shape in self.selectedShapes:
            self.selectedShapes.remove(shape)
        if shape in self.shapes:
            self.shapes.remove(shape)
        self.storeShapes()
        self.update()

    def duplicateSelectedShapes(self):
        if self.selectedShapes:
            self.selectedShapesCopy = [s.copy() for s in self.selectedShapes]
            self.boundedShiftShapes(self.selectedShapesCopy)
            self.endMove(copy=True)
        return self.selectedShapes

    def boundedShiftShapes(self, shapes):
        # Try to move in one direction, and if it fails in another.
        # Give up if both fail.
        point = shapes[0][0]
        offset = QtCore.QPointF(2.0, 2.0)
        self.offsets = QtCore.QPoint(), QtCore.QPoint()
        self.prevPoint = point
        if not self.boundedMoveShapes(shapes, point - offset):
            self.boundedMoveShapes(shapes, point + offset)
    
    def print_mask(self):
        for row in self.mask_label:
            print(' '.join(str(cell) for cell in row))

    def initialize_mask(self,width,height):
        return [[[0, 0] for _ in range(width)] for _ in range(height)]
    
    def set_mask_label(self, i, j, label):
        if label[0] == '0':
            self.mask_label[i][j] = [0, 0]
        elif label[0].isdigit():
            first_digit = int(label[0])
            second_digit = {'q': 1, 'w': 2, 'e': 3, 'r': 4}[label[1]]
            # self.debug_trace()
            self.mask_label[i][j] = [first_digit, second_digit]
        
    # def set_mask_label(self, i, j, label):
    #     if label[0] == '0':
    #         self.mask_label[i][j] = [0, 0]
    #     elif label[0].isdigit():
    #         first_digit = int(label[0])
    #         second_digit = {'q': 1, 'w': 2, 'e': 3, 'r': 4}[label[1]]
    #         if self.mask_label[i][j] == [first_digit, second_digit]:
    #             self.mask_label[i][j] = [0, 0]
    #         else:
    #             self.mask_label[i][j] = [first_digit, second_digit]

    def debug_trace(self):
        '''Set a tracepoint in the Python debugger that works with Qt'''
        from PyQt5.QtCore import pyqtRemoveInputHook
        from pdb import set_trace
        pyqtRemoveInputHook()
        set_trace()

    def paintEvent(self, event):
        if not self.pixmap:
            return super(Canvas, self).paintEvent(event)

        p = self._painter
        p.begin(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setRenderHint(QtGui.QPainter.HighQualityAntialiasing)
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)

        p.scale(self.scale, self.scale)
        p.translate(self.offsetToCenter())

        p.drawPixmap(0, 0, self.pixmap)

        # draw crosshair
        if (
            self._crosshair[self._createMode]
            and self.drawing()
            and self.prevMovePoint
            and not self.outOfPixmap(self.prevMovePoint)
        ):
            p.setPen(QtGui.QColor(0, 0, 0))
            p.drawLine(
                0,
                int(self.prevMovePoint.y()),
                self.width() - 1,
                int(self.prevMovePoint.y()),
            )
            p.drawLine(
                int(self.prevMovePoint.x()),
                0,
                int(self.prevMovePoint.x()),
                self.height() - 1,
            )

        if not self.shapes_visible:
            pen = QtGui.QPen(QtGui.QColor(0, 255, 0, 76))
        else:
            pen = QtGui.QPen(QtGui.QColor(0, 255, 0, 255))
        pen.setWidth(3)
        p.setPen(pen)
        width = self.pixmap.width()
        height = self.pixmap.height()
        h_step = width // self.patch_width
        v_step = height // self.patch_height
        for i in range(1, self.patch_width):
            p.drawLine(QPoint(i * h_step, 0), QPoint(i * h_step, height))
        for i in range(1, self.patch_height):
            p.drawLine(QPoint(0, i * v_step), QPoint(width, i * v_step))
        

        Shape.scale = self.scale
        if self.shapes_visible:
            for shape in self.shapes:
                if (shape.selected or not self._hideBackround) and self.isVisible(shape):
                    shape.fill = shape.selected or shape == self.hShape
                    if shape.shape_type != "patch_annotation":
                        #점찍은 곳에 색깔 x
                        shape.paint(p)

            if (self.fillDrawing() and self.createMode == "patch_annotation"):
                class_colors = {
                    0: QtGui.QColor(0, 0, 0, 0),
                    1: {1: QtGui.QColor(0, 255, 255, 90), 2: QtGui.QColor(0, 255, 255, 180)},
                    2: {1: QtGui.QColor(255, 255, 0, 90), 2: QtGui.QColor(255, 255, 0, 180)}, 
                    3: {1: QtGui.QColor(0, 0, 255, 90), 2: QtGui.QColor(0, 0, 255, 180)}, 
                    4: {1: QtGui.QColor(0, 255, 0, 90), 2: QtGui.QColor(0, 255, 0, 180)}, 
                    5: {1: QtGui.QColor(255, 0, 255, 90), 2: QtGui.QColor(255, 0, 255, 180)}, 
                    6: {1: QtGui.QColor(255, 0, 0, 90), 2: QtGui.QColor(255, 0, 0, 180)}, 
                }
                for shape in self.shapes:
                    if (shape.selected or not self._hideBackround) and self.isVisible(shape):
                        shape.fill = shape.selected or shape == self.hShape
                        shape.paint(p)

                        if shape.shape_type == "patch_annotation":
                            mask = shape_to_mask(
                                (self.pixmap.height(), self.pixmap.width()), shape.points,
                                shape_type="patch_annotation", patch_width=self.patch_width,
                                patch_height=self.patch_height
                            )
                            patch_size_h = self.pixmap.height() // self.patch_height
                            patch_size_w = self.pixmap.width() // self.patch_width
                            previous_mask = self.previous_masks.get(shape)

                            if previous_mask is None or not np.array_equal(mask, previous_mask):
                                self.previous_masks[shape] = mask
                                if mask.sum() != 0 and shape.label:
                                    indices = np.argwhere(mask)
                                    for idx in indices:
                                        self.set_mask_label(idx[0], idx[1], shape.label)

                if self.shapes:
                    mask_label_array = np.array(self.mask_label)
                    mask_nonzero_indices = np.argwhere(mask_label_array[:, :, 0] != 0)
                    labels = mask_label_array[mask_nonzero_indices[:, 0], mask_nonzero_indices[:, 1]]

                    for (i, j), label in zip(mask_nonzero_indices, labels):
                        first_digit = label[0]
                        second_digit = label[1]

                        if first_digit == 0:
                            color = class_colors[first_digit]
                        else:
                            color = class_colors[first_digit][second_digit]

                        p.fillRect(j * patch_size_w, i * patch_size_h, patch_size_w, patch_size_h, color)

                    #self.print_mask()
                    #print('\n')

        if self.current:
            self.current.paint(p)
            assert len(self.line.points) == len(self.line.point_labels)
            self.line.paint(p)
        if self.selectedShapesCopy:
            for s in self.selectedShapesCopy:
                s.paint(p)

        if (
            self.fillDrawing()
            and self.createMode == "polygon"
            and self.current is not None
            and len(self.current.points) >= 2
        ):
            drawing_shape = self.current.copy()
            if drawing_shape.fill_color.getRgb()[3] == 0:
                logger.warning(
                    "fill_drawing=true, but fill_color is transparent,"
                    " so forcing to be opaque."
                )
                drawing_shape.fill_color.setAlpha(64)
            drawing_shape.addPoint(self.line[1])
            drawing_shape.fill = True
            drawing_shape.paint(p)
        elif self.createMode == "ai_polygon" and self.current is not None:
            drawing_shape = self.current.copy()
            drawing_shape.addPoint(
                point=self.line.points[1],
                label=self.line.point_labels[1],
            )
            points = self._ai_model.predict_polygon_from_points(
                points=[[point.x(), point.y()] for point in drawing_shape.points],
                point_labels=drawing_shape.point_labels,
            )
            if len(points) > 2:
                drawing_shape.setShapeRefined(
                    shape_type="polygon",
                    points=[QtCore.QPointF(point[0], point[1]) for point in points],
                    point_labels=[1] * len(points),
                )
                drawing_shape.fill = self.fillDrawing()
                drawing_shape.selected = True
                drawing_shape.paint(p)
        elif self.createMode == "ai_mask" and self.current is not None:
            drawing_shape = self.current.copy()
            drawing_shape.addPoint(
                point=self.line.points[1],
                label=self.line.point_labels[1],
            )
            mask = self._ai_model.predict_mask_from_points(
                points=[[point.x(), point.y()] for point in drawing_shape.points],
                point_labels=drawing_shape.point_labels,
            )
            y1, x1, y2, x2 = imgviz.instances.masks_to_bboxes([mask])[0].astype(int)
            drawing_shape.setShapeRefined(
                shape_type="mask",
                points=[QtCore.QPointF(x1, y1), QtCore.QPointF(x2, y2)],
                point_labels=[1, 1],
                mask=mask[y1 : y2 + 1, x1 : x2 + 1],
            )
            drawing_shape.selected = True
            drawing_shape.paint(p)
        
        p.end()

    def transformPos(self, point):
        """Convert from widget-logical coordinates to painter-logical ones."""
        return point / self.scale - self.offsetToCenter()

    def offsetToCenter(self):
        s = self.scale
        area = super(Canvas, self).size()
        w, h = self.pixmap.width() * s, self.pixmap.height() * s
        aw, ah = area.width(), area.height()
        x = (aw - w) / (2 * s) if aw > w else 0
        y = (ah - h) / (2 * s) if ah > h else 0
        return QtCore.QPointF(x, y)

    def outOfPixmap(self, p):
        w, h = self.pixmap.width(), self.pixmap.height()
        return not (0 <= p.x() <= w - 1 and 0 <= p.y() <= h - 1)

    def finalise(self, drag=False):
        assert self.current
        if self.createMode == "ai_polygon":
            # convert points to polygon by an AI model
            assert self.current.shape_type == "points"
            points = self._ai_model.predict_polygon_from_points(
                points=[[point.x(), point.y()] for point in self.current.points],
                point_labels=self.current.point_labels,
            )
            self.current.setShapeRefined(
                points=[QtCore.QPointF(point[0], point[1]) for point in points],
                point_labels=[1] * len(points),
                shape_type="polygon",
            )
        elif self.createMode == "ai_mask":
            # convert points to mask by an AI model
            assert self.current.shape_type == "points"
            mask = self._ai_model.predict_mask_from_points(
                points=[[point.x(), point.y()] for point in self.current.points],
                point_labels=self.current.point_labels,
            )
            y1, x1, y2, x2 = imgviz.instances.masks_to_bboxes([mask])[0].astype(int)
            self.current.setShapeRefined(
                shape_type="mask",
                points=[QtCore.QPointF(x1, y1), QtCore.QPointF(x2, y2)],
                point_labels=[1, 1],
                mask=mask[y1 : y2 + 1, x1 : x2 + 1],
            )
        if self.createMode =="patch_annotation":
            self.current.close()
            self.shapes.append(self.current)
            self.storeShapes()
            self.current = None
            self.setHiding(False)
            
            # 창을 안띄우도록 바꿈
            self.newShape.emit('patch_anno')
            self.update()
        else:
            self.current.close()
            self.shapes.append(self.current)
            self.storeShapes()
            self.current = None
            self.setHiding(False)
            self.newShape.emit()
            self.update()


    def closeEnough(self, p1, p2):
        # d = distance(p1 - p2)
        # m = (p1-p2).manhattanLength()
        # print "d %.2f, m %d, %.2f" % (d, m, d - m)
        # divide by scale to allow more precision when zoomed in
        return labelme.utils.distance(p1 - p2) < (self.epsilon / self.scale)

    def intersectionPoint(self, p1, p2):
        # Cycle through each image edge in clockwise fashion,
        # and find the one intersecting the current line segment.
        # http://paulbourke.net/geometry/lineline2d/
        size = self.pixmap.size()
        points = [
            (0, 0),
            (size.width() - 1, 0),
            (size.width() - 1, size.height() - 1),
            (0, size.height() - 1),
        ]
        # x1, y1 should be in the pixmap, x2, y2 should be out of the pixmap
        x1 = min(max(p1.x(), 0), size.width() - 1)
        y1 = min(max(p1.y(), 0), size.height() - 1)
        x2, y2 = p2.x(), p2.y()
        d, i, (x, y) = min(self.intersectingEdges((x1, y1), (x2, y2), points))
        x3, y3 = points[i]
        x4, y4 = points[(i + 1) % 4]
        if (x, y) == (x1, y1):
            # Handle cases where previous point is on one of the edges.
            if x3 == x4:
                return QtCore.QPointF(x3, min(max(0, y2), max(y3, y4)))
            else:  # y3 == y4
                return QtCore.QPointF(min(max(0, x2), max(x3, x4)), y3)
        return QtCore.QPointF(x, y)

    def intersectingEdges(self, point1, point2, points):
        """Find intersecting edges.

        For each edge formed by `points', yield the intersection
        with the line segment `(x1,y1) - (x2,y2)`, if it exists.
        Also return the distance of `(x2,y2)' to the middle of the
        edge along with its index, so that the one closest can be chosen.
        """
        (x1, y1) = point1
        (x2, y2) = point2
        for i in range(4):
            x3, y3 = points[i]
            x4, y4 = points[(i + 1) % 4]
            denom = (y4 - y3) * (x2 - x1) - (x4 - x3) * (y2 - y1)
            nua = (x4 - x3) * (y1 - y3) - (y4 - y3) * (x1 - x3)
            nub = (x2 - x1) * (y1 - y3) - (y2 - y1) * (x1 - x3)
            if denom == 0:
                # This covers two cases:
                #   nua == nub == 0: Coincident
                #   otherwise: Parallel
                continue
            ua, ub = nua / denom, nub / denom
            if 0 <= ua <= 1 and 0 <= ub <= 1:
                x = x1 + ua * (x2 - x1)
                y = y1 + ua * (y2 - y1)
                m = QtCore.QPointF((x3 + x4) / 2, (y3 + y4) / 2)
                d = labelme.utils.distance(m - QtCore.QPointF(x2, y2))
                yield d, i, (x, y)

    # These two, along with a call to adjustSize are required for the
    # scroll area.
    def sizeHint(self):
        return self.minimumSizeHint()

    def minimumSizeHint(self):
        if self.pixmap:
            return self.scale * self.pixmap.size()
        return super(Canvas, self).minimumSizeHint()

    def wheelEvent(self, ev):
        if QT5:
            mods = ev.modifiers()
            delta = ev.angleDelta()
            if QtCore.Qt.ControlModifier == int(mods):
                # with Ctrl/Command key
                # zoom
                self.zoomRequest.emit(delta.y(), ev.pos())
            else:
                # scroll
                self.scrollRequest.emit(delta.x(), QtCore.Qt.Horizontal)
                self.scrollRequest.emit(delta.y(), QtCore.Qt.Vertical)
        else:
            if ev.orientation() == QtCore.Qt.Vertical:
                mods = ev.modifiers()
                if QtCore.Qt.ControlModifier == int(mods):
                    # with Ctrl/Command key
                    self.zoomRequest.emit(ev.delta(), ev.pos())
                else:
                    self.scrollRequest.emit(
                        ev.delta(),
                        QtCore.Qt.Horizontal
                        if (QtCore.Qt.ShiftModifier == int(mods))
                        else QtCore.Qt.Vertical,
                    )
            else:
                self.scrollRequest.emit(ev.delta(), QtCore.Qt.Horizontal)
        ev.accept()

    def moveByKeyboard(self, offset):
        if self.selectedShapes:
            self.boundedMoveShapes(self.selectedShapes, self.prevPoint + offset)
            #self.repaint()
            self.movingShape = True
    
    def keyPressEvent(self, ev):
        modifiers = ev.modifiers()
        key = ev.key()
        if ev.key() == QtCore.Qt.Key_U:
            self.mask_label = self.initialize_mask(self.patch_width, self.patch_height)
            self.reset_masklabel.emit()
            self.update()
        if ev.modifiers() & QtCore.Qt.ControlModifier:
            if ev.key() == QtCore.Qt.Key_C:
                Canvas.temp_mask_data = [row[:] for row in self.mask_label]
                # Canvas.temp_shape_data = self.shapes
                self.copy_masklabel.emit()

            elif ev.key() == QtCore.Qt.Key_V:
                if Canvas.temp_mask_data is not None:
                    self.mask_label = [row[:] for row in Canvas.temp_mask_data]
                    # self.shapes = Canvas.temp_shape_data
                    self.paste_masklabel.emit()
                    self.update()
        
        if ev.key() == QtCore.Qt.Key_Space:
            self.shapes_visible = not self.shapes_visible
            self.update()

        if self.drawing():
            if (self.class_text is not None) or (self.intensity_text is not None):
                if (self.class_text != "CLEAN") and (self.intensity_text != "CLEAN") and (self.class_text is not None):
                    self.tmp_class_text = self.class_text
                    self.tmp_intensity_text = self.intensity_text
            self.class_text = None
            self.intensity_text = None

            if key == QtCore.Qt.Key_Escape and self.current:
                self.current = None
                self.drawingPolygon.emit(False)
            elif key == QtCore.Qt.Key_Return and self.canCloseShape():
                self.finalise()
            elif modifiers == QtCore.Qt.AltModifier:
                self.snapping = False
            elif key in [QtCore.Qt.Key_1, QtCore.Qt.Key_2, QtCore.Qt.Key_3, QtCore.Qt.Key_4, QtCore.Qt.Key_5, QtCore.Qt.Key_6]:
                self.class_text = f"class{key - QtCore.Qt.Key_0}"
            elif key == QtCore.Qt.Key_Q:
                self.intensity_text = "BLURRY"
            elif key == QtCore.Qt.Key_W:
                self.intensity_text = "BLOCKAGE"
            elif key == QtCore.Qt.Key_X:
                self.class_text = "CLEAN"
                self.intensity_text = "CLEAN"

            if self.class_text or self.intensity_text:
                if (self.class_text != "CLEAN") and (self.intensity_text == "CLEAN"):
                    self.intensity_text = "BLURRY"
                self.classAndIntensityChanged.emit(self.class_text, self.intensity_text)
        elif self.editing():
            if (self.class_text is not None) or (self.intensity_text is not None):
                if (self.class_text != "CLEAN") and (self.intensity_text != "CLEAN") and (self.class_text is not None):
                    self.tmp_class_text = self.class_text
                    self.tmp_intensity_text = self.intensity_text
            self.class_text = None
            self.intensity_text = None
            if key == QtCore.Qt.Key_Up:
                self.moveByKeyboard(QtCore.QPointF(0.0, -MOVE_SPEED))
            elif key == QtCore.Qt.Key_Down:
                self.moveByKeyboard(QtCore.QPointF(0.0, MOVE_SPEED))
            elif key == QtCore.Qt.Key_Left:
                self.moveByKeyboard(QtCore.QPointF(-MOVE_SPEED, 0.0))
            elif key == QtCore.Qt.Key_Right:
                self.moveByKeyboard(QtCore.QPointF(MOVE_SPEED, 0.0))
            elif key in [QtCore.Qt.Key_1, QtCore.Qt.Key_2, QtCore.Qt.Key_3, QtCore.Qt.Key_4, QtCore.Qt.Key_5, QtCore.Qt.Key_6]:
                self.class_text = f"class{key - QtCore.Qt.Key_0}"
            elif key == QtCore.Qt.Key_Q:
                self.intensity_text = "BLURRY"
            elif key == QtCore.Qt.Key_W:
                self.intensity_text = "BLOCKAGE"
            elif key == QtCore.Qt.Key_X:
                self.class_text = "CLEAN"
                self.intensity_text = "CLEAN"

            if self.class_text or self.intensity_text:
                if (self.class_text != "CLEAN") and (self.intensity_text == "CLEAN"):
                    self.intensity_text = "BLURRY"
                self.classAndIntensityChanged.emit(self.class_text, self.intensity_text)

    def keyReleaseEvent(self, ev):
        modifiers = ev.modifiers()
        if ev.key() == QtCore.Qt.Key_Shift:
            # Reset box annotation mode if Shift is released
            if self.box_annotation_mode:
                self.box_annotation_mode = False
                self.box_start_point = None
                self.update()
                
            if self.createMode == "patch_annotation" and self.current:
                self.finalise()
        if ev.key() == QtCore.Qt.Key_X:
            self.class_text = self.tmp_class_text
            self.intensity_text = self.tmp_intensity_text
            if self.class_text or self.intensity_text:
                if (self.class_text != "CLEAN") and (self.intensity_text == "CLEAN") and (self.class_text is not None):
                    self.intensity_text = "BLURRY"
                self.classAndIntensityChanged.emit(self.class_text, self.intensity_text)
        if ev.key() == QtCore.Qt.Key_Space:
            self.shapes_visible = not self.shapes_visible
            self.update()
            
        if self.drawing():
            if int(modifiers) == 0:
                self.snapping = True
        elif self.editing():
            if self.movingShape and self.selectedShapes:
                index = self.shapes.index(self.selectedShapes[0])
                if self.shapesBackups[-1][index].points != self.shapes[index].points:
                    self.storeShapes()
                    self.shapeMoved.emit()

                self.movingShape = False

    def setLastLabel(self, text, flags):
        assert text
        self.shapes[-1].label = text
        self.shapes[-1].flags = flags
        self.shapesBackups.pop()
        self.storeShapes()
        return self.shapes[-1]

    def undoLastLine(self):
        assert self.shapes
        self.current = self.shapes.pop()
        self.current.setOpen()
        self.current.restoreShapeRaw()
        if self.createMode in ["polygon", "linestrip"]:
            self.line.points = [self.current[-1], self.current[0]]
        elif self.createMode in ["rectangle", "line", "circle"]:
            self.current.points = self.current.points[0:1]
        elif self.createMode == "point":
            self.current = None
        self.restoreMaskLabel()
        self.drawingPolygon.emit(True)

    def undoLastPoint(self):
        if not self.current or self.current.isClosed():
            return
        self.current.popPoint()
        if len(self.current) > 0:
            self.line[0] = self.current[-1]
        else:
            self.current = None
            self.drawingPolygon.emit(False)
        self.restoreMaskLabel()
        self.update()

    def loadPixmap(self, pixmap, clear_shapes=True):
        # Store current mask_label before changing pixmap
        old_mask_label = None
        old_previous_masks = None
        if hasattr(self, 'mask_label') and self.mask_label:
            old_mask_label = [row[:] for row in self.mask_label]
        if hasattr(self, 'previous_masks') and self.previous_masks:
            old_previous_masks = dict(self.previous_masks)
            
        self.pixmap = pixmap
        
        if self._ai_model:
            self._ai_model.set_image(
                image=labelme.utils.img_qt_to_arr(self.pixmap.toImage())
            )
        if clear_shapes:
            self.shapes = []
            # Reset mask_label when shapes are cleared
            self.mask_label = self.initialize_mask(self.patch_width, self.patch_height)
            self.previous_masks = {}  # Clear previous masks
        else:
            # Preserve mask_label when shapes are preserved (e.g., during brightness/contrast changes)
            if old_mask_label and len(old_mask_label) == self.patch_height and len(old_mask_label[0]) == self.patch_width:
                self.mask_label = old_mask_label
            else:
                self.mask_label = self.initialize_mask(self.patch_width, self.patch_height)
                
                # If dimensions changed, reapply annotations from shapes to new mask_label
                if self.shapes:
                    for shape in self.shapes:
                        if shape.shape_type == "patch_annotation" and shape.label:
                            mask = shape_to_mask(
                                (self.pixmap.height(), self.pixmap.width()), shape.points,
                                shape_type="patch_annotation", patch_width=self.patch_width,
                                patch_height=self.patch_height
                            )
                            if mask.sum() != 0:
                                indices = np.argwhere(mask)
                                for idx in indices:
                                    self.set_mask_label(idx[0], idx[1], shape.label)
            
            # Preserve previous_masks if shapes are preserved
            if old_previous_masks and not clear_shapes:
                self.previous_masks = old_previous_masks
        
        self.update()

    def loadShapes(self, shapes, replace=True):
        if replace:
            self.shapes = list(shapes)
        else:
            self.shapes.extend(shapes)
        self.storeShapes()
        self.current = None
        self.hShape = None
        self.hVertex = None
        self.hEdge = None
        self.update()

    def setShapeVisible(self, shape, value):
        self.visible[shape] = value
        self.update()

    def overrideCursor(self, cursor):
        self.restoreCursor()
        self._cursor = cursor
        QtWidgets.QApplication.setOverrideCursor(cursor)

    def restoreCursor(self):
        QtWidgets.QApplication.restoreOverrideCursor()

    def resetState(self):
        self.restoreCursor()
        self.pixmap = None
        self.shapesBackups = []
        self.update()

    def get_mask_label(self):
        return self.mask_label

    def annotateWithBox(self, start_point, end_point):
        """Annotate patches that are inside or touch the box/line formed by the two points."""
        if not start_point or not end_point:
            return
            
        # Create rectangle from the two points
        x1, y1 = start_point.x(), start_point.y()
        x2, y2 = end_point.x(), end_point.y()
        
        # In case the points are equal or form a line (no area)
        if x1 == x2 or y1 == y2:
            # Handle as a line annotation
            shape = Shape(shape_type="patch_annotation")
            if x1 == x2 and y1 == y2:
                # Single point - just add the point
                shape.addPoint(QtCore.QPointF(x1, y1))
                shape.close()
            else:
                # Line - add points along the line
                # Calculate the number of points to add based on the line length
                if x1 == x2:  # Vertical line
                    step = (y2 - y1) / max(abs(y2 - y1) / 5, 1)
                    for y in np.arange(y1, y2 + step, step):
                        shape.addPoint(QtCore.QPointF(x1, y))
                else:  # Horizontal line
                    step = (x2 - x1) / max(abs(x2 - x1) / 5, 1)
                    for x in np.arange(x1, x2 + step, step):
                        shape.addPoint(QtCore.QPointF(x, y1))
                shape.close()
        else:
            # Create a full rectangle
            # Normalize coordinates (in case of dragging from bottom to top or right to left)
            x_min, x_max = min(x1, x2), max(x1, x2)
            y_min, y_max = min(y1, y2), max(y1, y2)
            
            # Create a shape with points at the corners and inside the rectangle
            shape = Shape(shape_type="patch_annotation")
            
            # Add points at corners and at regular intervals inside the rectangle
            x_step = (x_max - x_min) / max(min(10, self.patch_width / 2), 1)
            y_step = (y_max - y_min) / max(min(10, self.patch_height / 2), 1)
            
            for x in np.arange(x_min, x_max + x_step, x_step):
                for y in np.arange(y_min, y_max + y_step, y_step):
                    shape.addPoint(QtCore.QPointF(x, y))
            
            shape.close()
        
        # Apply current class/intensity if set
        if hasattr(self, 'class_text') and hasattr(self, 'intensity_text') and self.class_text and self.intensity_text:
            if self.class_text.startswith("class") and len(self.class_text) > 5:
                class_digit = self.class_text[5]
                
                # Determine intensity character
                intensity_char = 'q'  # Default to 'q' (BLURRY)
                if self.intensity_text == "BLURRY":
                    intensity_char = 'q'
                elif self.intensity_text == "BLOCKAGE":
                    intensity_char = 'w'
                
                # Only set label if it's a valid class
                if class_digit.isdigit() and int(class_digit) > 0:
                    shape.label = f"{class_digit}{intensity_char}"
        
        # Add the shape to the list and store shapes
        self.shapes.append(shape)
        self.storeShapes()
        self.newShape.emit('patch_anno')
        self.update()