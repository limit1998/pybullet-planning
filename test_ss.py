import pybullet as p
import os
import argparse
import time
import pstats
import cProfile

from pybullet_utils import connect, add_data_path, disconnect, get_pose, get_body_names, \
    body_from_name, update_state, link_from_name, step_simulation
from problems import holding_problem, stacking_problem, cleaning_problem, cooking_problem

from ss.algorithms.dual_focused import dual_focused
from ss.algorithms.incremental import incremental
from ss.algorithms.plan_focused import plan_focused
from ss.model.functions import Predicate, NonNegFunction, rename_functions, initialize, TotalCost, Increase
from ss.model.problem import Problem, get_length, get_cost
from ss.model.operators import Action, Axiom
from ss.model.streams import Stream, ListStream, GenStream, FnStream, TestStream
from ss.model.bounds import PartialBoundFn, OutputSet
from ss.utils import INF

from streams import Pose, Conf, get_ik_ir_gen, get_motion_gen, get_stable_gen, \
    get_grasp_gen, Attach, Detach, Clean, Cook, Trajectory
from pr2_utils import ARM_LINK_NAMES, close_arm

A = '?a'
O = '?o'; O2 = '?o2'
#S = '?s' # Surface (in case I don't want to worry about stacking for real
P = '?p'; P2 = '?p2'
G = '?g'
BQ = '?q'; BQ2 = '?q2'
AT = '?at'; BT = '?bt'

IsArm = Predicate([A])
IsMovable = Predicate([O])
Stackable = Predicate([O, O2])

POSE = Predicate([P])
GRASP = Predicate([G])
IsBConf = Predicate([BQ])

IsPose = Predicate([O, P])
IsSupported = Predicate([P, O2])
IsGrasp = Predicate([O, G])
IsArmTraj = Predicate([AT])
IsBaseTraj = Predicate([BT])
IsKin = Predicate([A, O, P, G, BQ, AT])
IsReachable = Predicate([BQ])
IsMotion = Predicate([BQ, BQ2, BT])

AtPose = Predicate([O, P])
AtBConf = Predicate([BQ])
HandEmpty = Predicate([A])
HasGrasp = Predicate([A, O, G])
CanMove = Predicate([])

# Derived
Holding = Predicate([A, O])
On = Predicate([O, O2])
Unsafe = Predicate([AT])

Cleaned = Predicate([O])
Cooked = Predicate([O])
Washer = Predicate([O])
Stove = Predicate([O])

rename_functions(locals())


def ss_from_problem(problem, bound='cyclic'):
    robot = problem.robot

    initial_bq = Pose(robot, get_pose(robot))
    initial_atoms = [
        CanMove(),
        IsBConf(initial_bq), AtBConf(initial_bq),
        initialize(TotalCost(), 0),
    ]
    for name in problem.arms:
        initial_atoms += [IsArm(name), HandEmpty(name)]
    for body in problem.movable:
        pose = Pose(body, get_pose(body))
        initial_atoms += [IsMovable(body), IsPose(body, pose), AtPose(body, pose), POSE(pose)]
        for surface in problem.surfaces:
            initial_atoms += [Stackable(body, surface)]
    initial_atoms += map(Washer, problem.sinks)
    initial_atoms += map(Stove, problem.stoves)

    goal_literals = []
    if problem.goal_conf is not None:
        goal_conf = Pose(robot, problem.goal_conf)
        initial_atoms += [IsBConf(goal_conf)]
        goal_literals += [AtBConf(goal_conf)]
    goal_literals += [Holding(*pair) for pair in problem.goal_holding]
    goal_literals += [On(*pair) for pair in problem.goal_on]
    goal_literals += map(Cleaned, problem.goal_cleaned)
    goal_literals += map(Cooked, problem.goal_cooked)

    actions = [
        Action(name='pick', param=[A, O, P, G, BQ, AT],
               pre=[IsKin(A, O, P, G, BQ, AT),
                    HandEmpty(A), AtPose(O, P), AtBConf(BQ), ~Unsafe(AT)],
               eff=[HasGrasp(A, O, G), CanMove(), ~HandEmpty(A), ~AtPose(O, P),
                    Increase(TotalCost(), 1)]),

        Action(name='place', param=[A, O, P, G, BQ, AT],
               pre=[IsKin(A, O, P, G, BQ, AT),
                    HasGrasp(A, O, G), AtBConf(BQ), ~Unsafe(AT)],
               eff=[HandEmpty(A), CanMove(), AtPose(O, P), ~HasGrasp(A, O, G),
                    Increase(TotalCost(), 1)]),

        Action(name='move', param=[BQ, BQ2, BT],
               pre=[IsMotion(BQ, BQ2, BT),
                    CanMove(), AtBConf(BQ), ~Unsafe(BT)],
               eff=[AtBConf(BQ2), ~CanMove(), ~AtBConf(BQ),
                    Increase(TotalCost(), 1)]),

        Action(name='clean', param=[O, O2],  # Wirelessly communicates to clean
               pre=[Stackable(O, O2), Washer(O2),
                    ~Cooked(O), On(O, O2)],
               eff=[Cleaned(O)]),

        Action(name='cook', param=[O, O2],  # Wirelessly communicates to cook
               pre=[Stackable(O, O2), Stove(O2),
                    Cleaned(O), On(O, O2)],
               eff=[Cooked(O), ~Cleaned(O)]),
    ]
    axioms = [
        Axiom(param=[A, O, G],
              pre=[IsArm(A), IsGrasp(O, G),
                   HasGrasp(A, O, G)],
              eff=Holding(A, O)),
        Axiom(param=[O, P, O2],
              pre=[IsPose(O, P), IsSupported(P, O2),
                   AtPose(O, P)],
              eff=On(O, O2)),
    ]

    streams = [
        FnStream(name='motion', inp=[BQ, BQ2], domain=[IsBConf(BQ), IsBConf(BQ2)],
                 fn=get_motion_gen(problem), out=[BT],
                 graph=[IsMotion(BQ, BQ2, BT), IsBaseTraj(BT)], bound=bound),

        ListStream(name='grasp', inp=[O], domain=[IsMovable(O)], fn=get_grasp_gen(problem),
                   out=[G], graph=[IsGrasp(O, G), GRASP(G)], bound=bound),

        Stream(name='support', inp=[O, O2], domain=[Stackable(O, O2)],
               fn=get_stable_gen(problem), out=[P],
               graph=[IsPose(O, P), IsSupported(P, O2), POSE(P)], bound=bound),

        GenStream(name='ik_ir', inp=[A, O, P, G], domain=[IsArm(A), IsPose(O, P), IsGrasp(O, G)],
                  fn=get_ik_ir_gen(problem), out=[BQ, AT],
                  graph=[IsKin(A, O, P, G, BQ, AT), IsBConf(BQ), IsArmTraj(AT)],
                  bound=bound),
    ]

    return Problem(initial_atoms, goal_literals, actions, axioms, streams,
                   objective=TotalCost())

def post_process(problem, plan):
    if plan is None:
        return None
    robot = problem.robot
    commands = []
    for i, (action, args) in enumerate(plan):
        print i, action, args
        if action.name == 'move':
            t = args[-1]
            new_commands = [t]
        elif action.name == 'pick':
            a, b, p, g, _, t = args
            link = link_from_name(robot, ARM_LINK_NAMES[a])
            attach = Attach(robot, a, g, b)
            new_commands = [t, attach, t.reverse()]
        elif action.name == 'place':
            a, b, p, g, _, t = args
            link = link_from_name(robot, ARM_LINK_NAMES[a])
            detach = Detach(robot, a, b)
            new_commands = [t, detach, t.reverse()]
        elif action.name == 'clean': # TODO: add text or change color?
            body, sink = args
            new_commands = [Clean(body)]
        elif action.name == 'cook':
            body, stove = args
            new_commands = [Cook(body)]
        else:
            raise ValueError(action.name)
        commands += new_commands
    return commands

def step_commands(commands):
    # update_state()
    step_simulation()
    raw_input('Begin?')
    attachments = {}
    for i, command in enumerate(commands):
        print i, command
        if type(command) is Attach:
            attachments[command.body] = command
        elif type(command) is Detach:
            del attachments[command.body]
        elif type(command) is Trajectory:
            # for conf in command.path:
            for conf in command.path[1:]:
                conf.step()
                for attach in attachments.values():
                    attach.step()
                update_state()
                # print attachments
                step_simulation()
                raw_input('Continue?')
        elif type(command) in [Clean, Cook]:
            command.step()
        else:
            raise ValueError(command)

def main(search='ff-astar', max_time=30, verbose=False):
    parser = argparse.ArgumentParser()  # Automatically includes help
    parser.add_argument('-viewer', action='store_true', help='enable viewer.')
    args = parser.parse_args()
    problem_fn = cooking_problem # holding_problem | stacking_problem | cleaning_problem | cooking_problem

    #connect(use_gui=True)
    connect(use_gui=False)
    add_data_path()

    ss_problem = ss_from_problem(problem_fn())
    print ss_problem
    ss_problem.dump()

    #path = os.path.join('worlds', 'test_ss')
    #p.saveWorld(path)
    #state_id = p.saveState()
    #p.saveBullet(path)

    t0 = time.time()
    pr = cProfile.Profile()
    pr.enable()
    #plan, evaluations = incremental(ss_problem, planner=search, max_time=max_time,
    #                                verbose=verbose, terminate_cost=terminate_cost)
    plan, evaluations = dual_focused(ss_problem, planner=search, max_time=max_time,
                                     effort_weight=None, verbose=verbose)
    pr.disable()
    pstats.Stats(pr).sort_stats('tottime').print_stats(10) # tottime | cumtime

    print 'Plan:', plan
    print 'Solved:', plan is not None
    print 'Length:', get_length(plan, evaluations)
    print 'Cost:', get_cost(plan, evaluations)
    print 'Time:', time.time() - t0
    if (plan is None) or not args.viewer:
        return

    #state_id = p.saveState()
    #disconnect()
    #connect(use_gui=args.viewer)
    #p.restoreState(state_id)

    disconnect()
    connect(use_gui=args.viewer)

    #p.configureDebugVisualizer(p.COV_ENABLE_WIREFRAME, 1)
    p.configureDebugVisualizer(p.COV_ENABLE_GUI, 0) # Gets rid of GUI options
    #p.configureDebugVisualizer(p.COV_ENABLE_SHADOWS, 0)

    #p.configureDebugVisualizer(p.COV_ENABLE_RENDERING, 0)
    #p.configureDebugVisualizer(p.COV_ENABLE_TINY_RENDERER, 1)

    #p.configureDebugVisualizer(p.COV_ENABLE_RGB_BUFFER_PREVIEW, 0)
    #p.configureDebugVisualizer(p.COV_ENABLE_DEPTH_BUFFER_PREVIEW, 0)
    #p.configureDebugVisualizer(p.COV_ENABLE_SEGMENTATION_MARK_PREVIEW, 0)
    #p.configureDebugVisualizer(p.COV_ENABLE_VR_RENDER_CONTROLLERS, 0)
    #p.configureDebugVisualizer(p.COV_ENABLE_VR_PICKING, 0)
    #p.configureDebugVisualizer(p.COV_ENABLE_VR_TELEPORTING, 0)

    problem = problem_fn() # TODO: way of doing this without reloading?
    commands = post_process(problem, plan)
    step_commands(commands)
    raw_input('Finish?')
    disconnect()


if __name__ == '__main__':
    main()