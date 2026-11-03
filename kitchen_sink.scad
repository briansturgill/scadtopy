//
// OpenSCAD Kitchen Sink Test
// Intended for parser/transpiler validation
//

//--------------------------------------------------
// Globals
//--------------------------------------------------

$fn = 32;

pi2 = PI * 2;
number_val = 42;
float_val = 3.14159;
bool_val = true;
string_val = "hello world";

vector_val = [1,2,3];
nested_vector = [[1,2],[3,4],[5,[6,7]]];

range_val = [0:2:10];

//--------------------------------------------------
// Echo / Assert
//--------------------------------------------------

echo("starting");
echo(number_val);

assert(number_val == 42);
assert(len(vector_val) == 3);

//--------------------------------------------------
// Functions
//--------------------------------------------------

function square(x) = x * x;

function distance2d(x,y) =
    sqrt(square(x) + square(y));

function factorial(n) =
    n <= 1 ? 1 : n * factorial(n-1);

function fibonacci(n) =
    n < 2 ? n : fibonacci(n-1) + fibonacci(n-2);

function max3(a,b,c) =
    max(a, max(b,c));

function vector_sum(v) =
    len(v) == 0 ? 0 :
    v[0] + vector_sum(
        [for(i=[1:len(v)-1]) v[i]]
    );

//--------------------------------------------------
// Modules
//--------------------------------------------------

module marker(size=5)
{
    color("red")
    sphere(r=size);
}

module cube_with_hole(size=20,r=5)
{
    difference()
    {
        cube(size, center=true);

        rotate([90,0,0])
            cylinder(
                h=size*2,
                r=r,
                center=true
            );
    }
}

module repeated_object(count=5)
{
    for(i=[0:count-1])
    {
        translate([i*15,0,0])
            children();
    }
}

module conditional_shape(use_sphere=true)
{
    if(use_sphere)
        sphere(8);
    else
        cube(12, center=true);
}

//--------------------------------------------------
// Primitive Geometry
//--------------------------------------------------

translate([-80,0,0])
cube([10,20,30]);

translate([-60,0,0])
sphere(10);

translate([-40,0,0])
cylinder(h=25,r=8);

translate([-20,0,0])
cylinder(h=25,r1=5,r2=10);

//--------------------------------------------------
// Transform Stack
//--------------------------------------------------

translate([0,0,0])
rotate([15,25,35])
scale([1,2,0.5])
sphere(8);

//--------------------------------------------------
// Boolean Operations
//--------------------------------------------------

translate([40,0,0])
union()
{
    cube(15,center=true);
    sphere(10);
}

translate([70,0,0])
difference()
{
    sphere(12);
    cube(15,center=true);
}

translate([100,0,0])
intersection()
{
    sphere(12);
    cube(18,center=true);
}

//--------------------------------------------------
// Hull
//--------------------------------------------------

translate([-80,50,0])
hull()
{
    translate([-10,0,0]) sphere(5);
    translate([ 10,0,0]) sphere(5);
    translate([  0,15,0]) sphere(5);
}

//--------------------------------------------------
// Minkowski
//--------------------------------------------------

translate([-40,50,0])
minkowski()
{
    cube([10,10,10],center=true);
    sphere(2);
}

//--------------------------------------------------
// Let()
//--------------------------------------------------

translate([0,50,0])
let(
    a = 10,
    b = 20
)
cube([a,b,5],center=true);

//--------------------------------------------------
// List Comprehensions
//--------------------------------------------------

points =
[
    for(i=[0:10])
    [i, square(i)]
];

translate([40,50,0])
for(p = points)
{
    translate([p[0], p[1]/5, 0])
        sphere(0.8);
}

//--------------------------------------------------
// Nested Comprehension
//--------------------------------------------------

grid =
[
    for(x=[0:3])
        for(y=[0:3])
            [x,y]
];

translate([80,50,0])
for(p = grid)
{
    translate([p[0]*4,p[1]*4,0])
        cube(1);
}

//--------------------------------------------------
// Text
//--------------------------------------------------

translate([-80,100,0])
linear_extrude(height=2)
text(
    "OpenSCAD",
    size=8,
    halign="center"
);

//--------------------------------------------------
// Linear Extrude
//--------------------------------------------------

translate([-30,100,0])
linear_extrude(height=10, twist=180)
circle(5);

//--------------------------------------------------
// Rotate Extrude
//--------------------------------------------------

translate([20,100,0])
rotate_extrude(angle=270)
translate([15,0,0])
circle(2);

//--------------------------------------------------
// Offset
//--------------------------------------------------

translate([60,100,0])
linear_extrude(height=2)
offset(r=2)
square([10,10],center=true);

//--------------------------------------------------
// Projection
//--------------------------------------------------

translate([100,100,0])
projection(cut=false)
sphere(10);

//--------------------------------------------------
// Color
//--------------------------------------------------

translate([-80,150,0])
color([0,1,0,0.5])
cube(15);

//--------------------------------------------------
// Conditional Module
//--------------------------------------------------

translate([-40,150,0])
conditional_shape(true);

translate([-20,150,0])
conditional_shape(false);

//--------------------------------------------------
// Child Module Test
//--------------------------------------------------

translate([20,150,0])
repeated_object(4)
{
    cylinder(h=5,r=2);
}

//--------------------------------------------------
// Polygon
//--------------------------------------------------

translate([60,150,0])
linear_extrude(5)
polygon(
    points=[
        [0,0],
        [20,0],
        [10,15]
    ],
    paths=[[0,1,2]]
);

//--------------------------------------------------
// Polyhedron
//--------------------------------------------------

translate([100,150,0])
polyhedron(
    points=[
        [0,0,0],
        [10,0,0],
        [0,10,0],
        [0,0,10]
    ],
    faces=[
        [0,1,2],
        [0,1,3],
        [0,2,3],
        [1,2,3]
    ]
);

//--------------------------------------------------
// Named Parameters
//--------------------------------------------------

translate([-80,200,0])
cube_with_hole(
    r=3,
    size=18
);

//--------------------------------------------------
// Surface / Import Examples
//--------------------------------------------------

// surface(file="heightmap.dat");
//
// import("model.stl");
//
// import("drawing.svg");
//
// include <constants.scad>;
//
// use <library.scad>;

//--------------------------------------------------
// Recursive Function Tests
//--------------------------------------------------

echo("factorial(5)", factorial(5));
echo("fibonacci(10)", fibonacci(10));

//--------------------------------------------------
// Ternary
//--------------------------------------------------

test_value =
    bool_val ? 111 : 222;

echo(test_value);

//--------------------------------------------------
// End
//--------------------------------------------------

echo("done");
