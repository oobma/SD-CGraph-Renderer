bl_info = {
    "name" : "SD CGraph Renderer",
    "author" : "oobma (Gemini, Final Architect)",
    "description" : "Renders SD_CGraph lines with custom colors",
    "blender" : (4, 0, 0),
    "version" : (0, 1, 0),
    "location" : "3D View > Sidebar > SD CGraph Renderer",
    "category" : "3D View"
}

import bpy
import gpu
from gpu_extras.batch import batch_for_shader

# State Variables
draw_handler = None
is_drawing_active = False
# Internal name to identify the node injected by this addon
AUTO_NODE_NAME = "SNA_Auto_CurveToMesh"

# ------------------------------------------------------------------------
#   AUTO NODE MANAGEMENT
# ------------------------------------------------------------------------

def find_modifier_and_tree(obj):
    """Helper to find the SD_CGraph modifier and its node tree."""
    for mod in obj.modifiers:
        if mod.type == 'NODES' and mod.node_group:
            # Case insensitive check
            if "cgraph" in mod.node_group.name.lower():
                return mod, mod.node_group
    return None, None

def inject_curve_to_mesh(obj):
    """
    Inserts a 'Curve to Mesh' node just before the Group Output if not present.
    Required for Python to read the geometry edges.
    """
    mod, tree = find_modifier_and_tree(obj)
    if not tree: return

    # 1. Check if our auto node already exists
    if AUTO_NODE_NAME in tree.nodes:
        return # Already injected

    # 2. Find the active Group Output
    output_node = None
    for node in tree.nodes:
        if node.type == 'GROUP_OUTPUT' and node.is_active_output:
            output_node = node
            break
    
    if not output_node:
        # Fallback: find any output
        for node in tree.nodes:
            if node.type == 'GROUP_OUTPUT':
                output_node = node
                break
    
    if not output_node: return

    # 3. Analyze the Geometry input socket of the Output node
    socket_in = output_node.inputs[0] # Assuming first input is Geometry
    if not socket_in.is_linked:
        return # Nothing connected, we can't inject in between

    link = socket_in.links[0]
    from_socket = link.from_socket
    
    # 4. Create the Curve To Mesh node
    c2m_node = tree.nodes.new("GeometryNodeCurveToMesh")
    c2m_node.name = AUTO_NODE_NAME
    c2m_node.label = "Auto Renderer Fix"
    c2m_node.select = False
    
    # Position it nicely near the output
    c2m_node.location = (output_node.location.x - 200, output_node.location.y)

    # 5. Reconnect links
    tree.links.remove(link) # Remove old link
    tree.links.new(from_socket, c2m_node.inputs['Curve']) # Previous -> New
    tree.links.new(c2m_node.outputs['Mesh'], socket_in)   # New -> Output

def remove_curve_to_mesh(obj):
    """
    Finds the injected node, bridges the connection back, and deletes the node.
    Restores the tree to its original state.
    """
    mod, tree = find_modifier_and_tree(obj)
    if not tree: return

    if AUTO_NODE_NAME not in tree.nodes:
        return

    node_to_del = tree.nodes[AUTO_NODE_NAME]
    
    # Bridge connections (Input: Curve -> Output: Mesh)
    input_socket = node_to_del.inputs['Curve']
    output_socket = node_to_del.outputs['Mesh']
    
    if input_socket.is_linked and output_socket.is_linked:
        from_socket = input_socket.links[0].from_socket
        # Output might have multiple links
        for link in output_socket.links:
            to_socket = link.to_socket
            tree.links.new(from_socket, to_socket)
            
    tree.nodes.remove(node_to_del)


# ------------------------------------------------------------------------
#   DRAWING LOGIC
# ------------------------------------------------------------------------

def draw_lines_callback():
    # Wrap in try-except to avoid context errors during file load/close
    try:
        scene = bpy.context.scene
        depsgraph = bpy.context.evaluated_depsgraph_get()
    except: return

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    shader.bind()
    
    # Global Thickness
    gpu.state.line_width_set(getattr(scene, "sd_cgraph_thickness", 2.0))
    
    # X-Ray Mode
    if getattr(scene, "sd_cgraph_always_on_top", True):
        gpu.state.depth_test_set('NONE')
    else:
        gpu.state.depth_test_set('LESS_EQUAL')

    # Iterate objects
    for obj in scene.objects:
        if not obj.visible_get() or obj.type not in {'MESH', 'CURVE'}:
            continue
            
        # Fast filter
        is_candidate = False
        for mod in obj.modifiers:
            if mod.type == 'NODES' and mod.node_group and "cgraph" in mod.node_group.name.lower():
                is_candidate = True
                break
        
        if not is_candidate: continue

        # Get Geometry (Edges from the injected Curve to Mesh)
        eval_obj = obj.evaluated_get(depsgraph)
        try:
            mesh = eval_obj.to_mesh()
        except RuntimeError:
            continue

        if mesh and len(mesh.edges) > 0:
            coords = []
            world_mat = obj.matrix_world
            
            verts_co = [world_mat @ v.co for v in mesh.vertices]
            for edge in mesh.edges:
                coords.append(verts_co[edge.vertices[0]])
                coords.append(verts_co[edge.vertices[1]])
            
            # Individual Object Color
            obj_color = getattr(obj, "sd_cgraph_color", (0, 1, 1, 1))
            shader.uniform_float("color", obj_color)
            
            batch = batch_for_shader(shader, 'LINES', {"pos": coords})
            batch.draw(shader)

        eval_obj.to_mesh_clear()

    # Restore GPU State
    if getattr(scene, "sd_cgraph_always_on_top", True):
        gpu.state.depth_test_set('LESS_EQUAL')
    gpu.state.line_width_set(1.0)


# ------------------------------------------------------------------------
#   OPERATORS
# ------------------------------------------------------------------------

class SNA_OT_ToggleRenderer(bpy.types.Operator):
    bl_idname = "sna.toggle_sd_renderer"
    bl_label = "Toggle Renderer"
    bl_description = "Enables or disables the custom line drawing and node management"
    
    def execute(self, context):
        global draw_handler, is_drawing_active
        
        scene_objects = context.scene.objects
        
        if is_drawing_active:
            # --- DISABLE ---
            if draw_handler:
                try: bpy.types.SpaceView3D.draw_handler_remove(draw_handler, 'WINDOW')
                except: pass
            draw_handler = None
            is_drawing_active = False
            
            # Cleanup nodes
            for obj in scene_objects:
                remove_curve_to_mesh(obj)
                
            self.report({'INFO'}, "SD CGraph Renderer: Disabled")
            
        else:
            # --- ENABLE ---
            # Inject nodes
            for obj in scene_objects:
                inject_curve_to_mesh(obj)
                
            if draw_handler is None:
                draw_handler = bpy.types.SpaceView3D.draw_handler_add(draw_lines_callback, (), 'WINDOW', 'POST_VIEW')
            is_drawing_active = True
            
            self.report({'INFO'}, "SD CGraph Renderer: Enabled")
            
        # Redraw
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
                    
        return {"FINISHED"}

class SNA_OT_RefreshNodes(bpy.types.Operator):
    bl_idname = "sna.refresh_sd_nodes"
    bl_label = "Refresh Nodes"
    bl_description = "Refreshes the node injection for newly added objects"
    
    def execute(self, context):
        if is_drawing_active:
            for obj in context.scene.objects:
                inject_curve_to_mesh(obj)
            context.area.tag_redraw()
            self.report({'INFO'}, "SD CGraph Renderer: Nodes Refreshed")
        return {'FINISHED'}

# ------------------------------------------------------------------------
#   UI PANEL
# ------------------------------------------------------------------------

class SNA_PT_RendererPanel(bpy.types.Panel):
    bl_label = "SD CGraph Renderer"
    bl_idname = "SNA_PT_RendererPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "SD CGraph Renderer" # Sidebar Tab Name

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        obj = context.active_object
        
        if is_drawing_active:
            row = layout.row(align=True)
            row.operator(SNA_OT_ToggleRenderer.bl_idname, text="Disable", icon='PAUSE', depress=True)
            row.operator(SNA_OT_RefreshNodes.bl_idname, text="", icon='FILE_REFRESH')
        else:
            layout.operator(SNA_OT_ToggleRenderer.bl_idname, text="Enable", icon='PLAY')

        layout.separator()
        
        # Object Properties
        box = layout.box()
        box.label(text="Selected Object Color:")
        
        if obj:
            has_cgraph = False
            for mod in obj.modifiers:
                if mod.type == 'NODES' and mod.node_group:
                    if "cgraph" in mod.node_group.name.lower():
                        has_cgraph = True
                        break
            
            if has_cgraph:
                row = box.row()
                row.prop(obj, "sd_cgraph_color", text="")
                row.label(text=obj.name, icon='OBJECT_DATA')
            else:
                box.label(text="No CGraph found", icon='INFO')
        else:
            box.label(text="No selection")

        # Scene Properties
        layout.separator()
        col = layout.column()
        col.label(text="Global Settings:")
        col.prop(scene, "sd_cgraph_thickness", slider=True)
        col.prop(scene, "sd_cgraph_always_on_top", toggle=True)

# ------------------------------------------------------------------------
#   REGISTRATION
# ------------------------------------------------------------------------

classes = (
    SNA_OT_ToggleRenderer,
    SNA_OT_RefreshNodes,
    SNA_PT_RendererPanel,
)

def register():
    # Per-Object property
    bpy.types.Object.sd_cgraph_color = bpy.props.FloatVectorProperty(
        name="Color", subtype='COLOR', default=(0.0, 1.0, 0.0, 1.0),
        size=4, min=0.0, max=1.0
    )
    # Global properties
    bpy.types.Scene.sd_cgraph_thickness = bpy.props.FloatProperty(
        name="Thickness", default=2.0, min=1.0, max=10.0
    )
    bpy.types.Scene.sd_cgraph_always_on_top = bpy.props.BoolProperty(
        name="Always on Top", default=True
    )

    for cls in classes:
        bpy.utils.register_class(cls)

def unregister():
    global draw_handler, is_drawing_active
    
    # Try to cleanup nodes on removal
    if is_drawing_active:
        try:
            for obj in bpy.data.objects:
                remove_curve_to_mesh(obj)
        except: pass

    if is_drawing_active and draw_handler:
        try: bpy.types.SpaceView3D.draw_handler_remove(draw_handler, 'WINDOW')
        except: pass
        
    draw_handler = None
    is_drawing_active = False
    
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
        
    del bpy.types.Object.sd_cgraph_color
    del bpy.types.Scene.sd_cgraph_thickness
    del bpy.types.Scene.sd_cgraph_always_on_top

if __name__ == "__main__":
    register()